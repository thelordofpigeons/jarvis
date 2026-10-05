"""Collector protocol, window logic and the runner (design section 8).

A collector turns one source into `CollectResult`. Collectors never raise: the runner
converts an exception or a timeout into `CollectResult(ok=False, error=...)` so one broken
source becomes a "Source status" line and the digest carries on.

Layer L3. Collectors read files only through `tier.safe_read_text` (an AST test in
tests/test_write_locations.py enforces it) and never write anywhere.

Limits stated plainly:
- A timed-out collector cannot be killed. Its thread is a daemon thread, so it cannot hold
  the process open, but it may keep running and finish in the background. Collectors are
  read-only, so a straggler costs time, not safety.
- The error text of a failed collector is the exception class name only. Exception messages
  often carry file names, and a result may end up in the digest note.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from jarvisd.common import short_id
from jarvisd.config import Config
from jarvisd.models import CollectResult, WithheldItem
from jarvisd.state import StateStore

DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class CollectContext:
    """Everything a collector may know about this run.

    `now` must carry the local zone (use common.local_now()): "today", "overdue" and the age
    of a thread are all judged against the local calendar date.
    """

    cfg: Config
    window_start: datetime
    window_end: datetime
    now: datetime
    # The digest job that asked, so a collector that makes a paid call can have it audited and
    # costed to that job. None for callers outside a job (jarvis ask, tests).
    job_id: str | None = None


@runtime_checkable
class Collector(Protocol):
    """One source. `name` is the CollectResult.source it produces."""

    name: str

    def collect(self, ctx: CollectContext) -> CollectResult: ...


def compute_window(state: StateStore, cfg: Config, now: datetime) -> tuple[datetime, datetime]:
    """(start, end) of this digest's window: since the last success, capped, else the default.

    The watermark only moves after a successful vault write, so a failed day widens the next
    window instead of losing it. The cap keeps a long absence from producing a huge payload.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("compute_window needs an aware 'now'")
    mark = state.watermark.get()
    if mark is None:
        start = now - timedelta(hours=cfg.digest.window_hours_default)
    else:
        start = max(mark, now - timedelta(hours=cfg.digest.window_hours_max))
    # A watermark in the future (clock moved back) must not produce a negative window.
    return min(start, now), now


def withheld_ref(kind: str, source_ref: str, reason: str) -> WithheldItem:
    """A content-free pointer in the design's `w-xxxxxx` id style (D5). Local use only."""
    return WithheldItem(
        id="w-" + short_id(kind, source_ref)[:6],
        kind=kind,
        source_ref=source_ref,
        reason=reason,
        hold_kind="sensitive",
    )


def _failure(name: str, error: str, started: float) -> CollectResult:
    return CollectResult(source=name, ok=False, error=error, duration_ms=_elapsed_ms(started))


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _run_one(ctx: CollectContext, collector: Collector, timeout_s: float) -> CollectResult:
    name = str(getattr(collector, "name", type(collector).__name__))
    # A collector that waits on a paid call (ClickUp) says how long it needs.
    timeout_s = float(getattr(collector, "timeout_s", timeout_s))
    started = time.monotonic()
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["result"] = collector.collect(ctx)
        except BaseException as exc:  # noqa: BLE001  a collector must never take the run down
            box["error"] = type(exc).__name__

    worker = threading.Thread(target=target, name=f"collect-{name}", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return _failure(name, f"timeout after {timeout_s:g}s", started)
    if "error" in box:
        return _failure(name, str(box["error"]), started)
    res = box.get("result")
    if not isinstance(res, CollectResult):
        return _failure(name, "bad_result", started)
    return res.model_copy(update={"duration_ms": _elapsed_ms(started)})


def run_collectors(
    ctx: CollectContext,
    collectors: Sequence[Collector],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[CollectResult]:
    """Run each collector in order, one result per collector, never raising."""
    return [_run_one(ctx, collector, timeout_s) for collector in collectors]
