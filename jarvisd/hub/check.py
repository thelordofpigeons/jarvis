"""`jarvis hub --check`: build the app and render every view against the configured state.

It uses the real paths from the loaded configuration, so on the owner's machine it reads the
live state, queue, audit log and digest notes, with the same read-only data layer the server
uses. Nothing is started, nothing listens, and nothing is written.

Layer L3 (hub).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi.testclient import TestClient

from jarvisd.config import Config
from jarvisd.hub.app import LOOPBACK_HOST, create_app

VIEWS = (("Today", "/"), ("Runs", "/runs"), ("Held", "/held"), ("Repos", "/repos"), ("Projects", "/projects"),
         ("Ledger", "/ledger"), ("Audit", "/audit"),
         ("Status", "/status"))


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def run_check(cfg: Config, *, clock: Callable[[], datetime] | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        app = create_app(cfg, clock=clock)
    except Exception as exc:  # noqa: BLE001 - a self-test reports whatever broke
        return [CheckResult("App", False, f"{type(exc).__name__}: {exc}")]
    results.append(CheckResult("App", True, "built"))
    client = TestClient(app, base_url=f"http://{LOOPBACK_HOST}:{cfg.hub.port}")
    for name, path in VIEWS:
        try:
            resp = client.get(path)
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(name, False, f"{path} raised {type(exc).__name__}: {exc}"))
            continue
        ok = resp.status_code == 200 and '<main id="main">' in resp.text
        results.append(CheckResult(name, ok, f"{path} {resp.status_code}, {len(resp.text)} bytes"))
    try:
        post = client.post("/")
        results.append(CheckResult("Read-only", post.status_code == 405, f"POST / answers {post.status_code}"))
        foreign = TestClient(app, base_url="http://not-allowed.invalid").get("/status")
        results.append(CheckResult("Host guard", foreign.status_code == 403, f"foreign Host answers {foreign.status_code}"))
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("Guards", False, f"{type(exc).__name__}: {exc}"))
    return results


def format_results(results: list[CheckResult]) -> list[str]:
    return [f"{'PASS' if r.ok else 'FAIL'}  {r.name:<11} {r.detail}".rstrip() for r in results]
