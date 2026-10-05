#!/usr/bin/env python3
"""JARVIS watchdog (report sections 1 and 9c).

Runs under the HUMAN account, never the daemon account. Three jobs:

  1. Health probe    poll llama-server, restart it on sustained failure, with a
                     per-hour budget so a crash loop notifies instead of looping.
  2. GPU-yield sensor detect a foreground app that wants the GPU and yield it,
                     per the "resident while awake" and GPU yield rules.
  3. Kill switch      trip bin/kill-switch.ps1, either directly or through the
                     elevated JarvisKillSwitch scheduled task created in phase 0.

Standard library only, so it runs on a cold machine with no virtualenv.

Phase 0 note: jarvisd and llama-server do not exist yet. Absent targets are
reported as 'absent', never as failures, so this is safe to run now.

Usage
  python bin/watchdog.py --self-test        validate config and dependencies
  python bin/watchdog.py --once             one probe cycle, print the verdict
  python bin/watchdog.py                    resident loop
  python bin/watchdog.py --trip "reason"    trip the kill switch by hand
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "jarvis.toml"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expand_path(value: str, base: Path = ROOT) -> str:
    """Same rule as jarvisd.config.expand_path: `~` is home, relative means repo-relative.

    Duplicated on purpose: this file is standard library only so it runs on a cold machine.
    """
    text = str(value)
    if text == "~" or text.startswith(("~/", "~\\")):
        text = os.path.expanduser("~") + text[1:]
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    return os.path.normpath(path)


# Keys that hold a path, per table. Everything else in the file is left as written.
_PATH_KEYS = {
    "paths": ("root", "queue", "logs", "models", "vault_write_raw", "vault_write_sessions"),
    "killswitch": ("audit_log",),
    "sandbox": ("srt_path", "settings_path"),
    "audit": ("log",),
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    base = path.parent
    for table, keys in _PATH_KEYS.items():
        section = cfg.get(table)
        if not isinstance(section, dict):
            continue
        for key in keys:
            if isinstance(section.get(key), str):
                section[key] = expand_path(section[key], base)
    forbidden = cfg.get("paths", {}).get("vault_forbidden")
    if isinstance(forbidden, list):
        cfg["paths"]["vault_forbidden"] = [expand_path(v, base) for v in forbidden]
    return cfg


class JsonlLog:
    """Append-only event log. A failure to log is reported but never fatal."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields) -> None:
        record = {"ts": now_iso(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print(f"[warn] log write failed: {exc}", file=sys.stderr)
        print(line, flush=True)


def probe_health(url: str, timeout: float = 4.0) -> tuple[str, str]:
    """Return (status, detail). status is ok, unhealthy or absent.

    A refused connection means the server is not running, which in phase 0 is
    the expected state and must not be mistaken for a crash.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            code = resp.getcode()
            if 200 <= code < 300:
                return "ok", f"http {code}"
            return "unhealthy", f"http {code}"
    except urllib.error.HTTPError as exc:
        return "unhealthy", f"http {exc.code}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        text = str(reason).lower()
        if "refused" in text or "target machine actively refused" in text:
            return "absent", "connection refused, server not running"
        return "unhealthy", str(reason)
    except (TimeoutError, OSError) as exc:
        return "unhealthy", str(exc)


def running_processes() -> list[dict]:
    """List processes via tasklist. Returns [] if tasklist is unavailable."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError, FileNotFoundError) as exc:
        print(f"[warn] tasklist failed: {exc}", file=sys.stderr)
        return []
    procs = []
    for row in csv.reader(out.splitlines()):
        if len(row) >= 2:
            procs.append({"name": row[0], "pid": row[1]})
    return procs


def gpu_contention(watch_names: list[str]) -> list[str]:
    """Names of running processes that we treat as wanting the GPU.

    Limitation, stated rather than hidden: actual VRAM headroom is not readable
    on AMD under Windows without a vendor tool, so this is presence-based only.
    The vram headroom threshold in jarvis.toml is not enforced yet.
    """
    wanted = {n.lower() for n in watch_names}
    hits = set()
    for proc in running_processes():
        stem = os.path.splitext(proc["name"])[0].lower()
        if stem in wanted:
            hits.add(stem)
    return sorted(hits)


class RestartBudget:
    """Allow at most `limit` restarts per rolling hour."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.stamps: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self.stamps and now - self.stamps[0] > 3600:
            self.stamps.popleft()

    def allow(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        self._prune(now)
        return len(self.stamps) < self.limit

    def record(self, now: float | None = None) -> None:
        self.stamps.append(time.time() if now is None else now)

    @property
    def used(self) -> int:
        self._prune(time.time())
        return len(self.stamps)


def trip_kill_switch(cfg: dict, log: JsonlLog, reason: str, scope: str = "Daemon",
                     dry_run: bool = False) -> int:
    """Trip the kill switch.

    Prefers the elevated scheduled task, because the firewall, service and
    Tailscale steps need Administrator and the watchdog itself does not run
    elevated. Falls back to calling the script directly, which still stops the
    daemon task and its processes.
    """
    task = cfg.get("killswitch", {}).get("scheduled_task", "JarvisKillSwitch")
    script = ROOT / "bin" / "kill-switch.ps1"

    probe = subprocess.run(["schtasks", "/query", "/tn", task],
                           capture_output=True, text=True)
    if probe.returncode == 0 and not dry_run:
        log.write("killswitch_trip", via="scheduled_task", task=task, reason=reason, scope=scope)
        result = subprocess.run(["schtasks", "/run", "/tn", task],
                                capture_output=True, text=True)
        log.write("killswitch_result", via="scheduled_task", rc=result.returncode,
                  stdout=result.stdout.strip(), stderr=result.stderr.strip())
        return result.returncode

    if probe.returncode != 0:
        log.write("killswitch_task_absent", task=task,
                  note="elevated task not registered, see docs/phase0-runbook.md")

    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-Scope", scope, "-Reason", reason]
    if dry_run:
        args.append("-DryRun")
    log.write("killswitch_trip", via="direct", reason=reason, scope=scope, dry_run=dry_run)
    result = subprocess.run(args, capture_output=True, text=True)
    log.write("killswitch_result", via="direct", rc=result.returncode,
              stdout=result.stdout.strip()[-2000:], stderr=result.stderr.strip()[-2000:])
    return result.returncode


def restart_llama(cfg: dict, log: JsonlLog) -> bool:
    """Restart the inference server. Phase 1 wires this to llama-swap."""
    task = cfg.get("llama", {}).get("swap_scheduled_task", "JarvisLlamaSwap")
    probe = subprocess.run(["schtasks", "/query", "/tn", task], capture_output=True, text=True)
    if probe.returncode != 0:
        log.write("restart_skipped", target=task, reason="task not registered yet (phase 1)")
        return False
    result = subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True, text=True)
    log.write("restart_attempt", target=task, rc=result.returncode,
              stderr=result.stderr.strip())
    return result.returncode == 0


def cycle(cfg: dict, log: JsonlLog, state: dict) -> dict:
    """One observation cycle. Returns the verdict dict."""
    wd = cfg.get("watchdog", {})
    status, detail = probe_health(wd.get("health_url", "http://127.0.0.1:8080/health"))
    contention = gpu_contention(wd.get("gpu_yield_processes", [])) if wd.get("gpu_yield_enabled", True) else []

    if status == "ok":
        state["fails"] = 0
    elif status == "unhealthy":
        state["fails"] = state.get("fails", 0) + 1
    else:
        state["fails"] = 0  # absent is not a failure

    verdict = {
        "health": status,
        "detail": detail,
        "consecutive_fails": state.get("fails", 0),
        "gpu_contention": contention,
        "restarts_used_this_hour": state["budget"].used,
    }

    threshold = int(wd.get("health_fail_threshold", 3))
    if status == "unhealthy" and state["fails"] >= threshold:
        if state["budget"].allow():
            state["budget"].record()
            verdict["action"] = "restart"
            log.write("health_degraded", **verdict)
            restart_llama(cfg, log)
            state["fails"] = 0
            time.sleep(float(wd.get("restart_backoff_seconds", 30)))
        else:
            verdict["action"] = "restart_budget_exhausted"
            log.write("health_crashloop", **verdict,
                      note="restart budget spent, not restarting again this hour")
    elif contention:
        verdict["action"] = "gpu_yield"
        log.write("gpu_yield", **verdict)
    else:
        verdict["action"] = "none"
        log.write("probe", **verdict)

    return verdict


def self_test(cfg: dict) -> int:
    checks: list[tuple[str, bool, str]] = []

    checks.append(("config-loads", True, str(CONFIG_PATH)))
    for section in ("paths", "gates", "router", "watchdog", "killswitch"):
        checks.append((f"section-{section}", section in cfg, ""))

    ks = ROOT / "bin" / "kill-switch.ps1"
    checks.append(("killswitch-script-exists", ks.is_file(), str(ks)))

    for key in ("logs", "queue", "models"):
        p = Path(cfg["paths"][key])
        # Gitignored runtime folders: a fresh clone has none, so create them rather than fail.
        p.mkdir(parents=True, exist_ok=True)
        checks.append((f"dir-{key}", p.is_dir(), str(p)))

    thr = cfg["gates"].get("confidence_threshold")
    checks.append(("confidence-threshold-sane", isinstance(thr, float) and 0 < thr < 1, str(thr)))

    fields = set(cfg["router"].get("contract_fields", []))
    required = {"category", "sensitive", "importance", "confidence", "needs_tools", "language", "reason"}
    checks.append(("router-contract-complete", required <= fields,
                   "missing: " + ", ".join(sorted(required - fields)) if required - fields else "all 7"))

    procs = running_processes()
    checks.append(("tasklist-works", len(procs) > 0, f"{len(procs)} processes"))

    status, detail = probe_health(cfg["watchdog"]["health_url"], timeout=2.0)
    checks.append(("health-probe-callable", status in {"ok", "unhealthy", "absent"},
                   f"{status}: {detail}"))

    budget = RestartBudget(2)
    ok = budget.allow() and (budget.record() or budget.allow()) and (budget.record() or not budget.allow())
    checks.append(("restart-budget-enforced", ok, "limit 2 honoured"))

    failed = 0
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}".rstrip())
        if not passed:
            failed += 1
    print(f"\nself-test: {len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="JARVIS watchdog")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--self-test", action="store_true", help="validate config and dependencies")
    ap.add_argument("--trip", metavar="REASON", help="trip the kill switch and exit")
    ap.add_argument("--scope", default="Daemon", choices=["Daemon", "Full"])
    ap.add_argument("--dry-run", action="store_true", help="with --trip, do not act")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"[fatal] cannot load {CONFIG_PATH}: {exc}", file=sys.stderr)
        return 2

    if args.self_test:
        return self_test(cfg)

    log = JsonlLog(Path(cfg["paths"]["logs"]) / "watchdog.jsonl")

    if args.trip:
        return trip_kill_switch(cfg, log, reason=args.trip, scope=args.scope,
                                dry_run=args.dry_run)

    state = {"fails": 0,
             "budget": RestartBudget(int(cfg["watchdog"].get("max_restarts_per_hour", 4)))}

    if args.once:
        cycle(cfg, log, state)
        return 0

    interval = float(cfg["watchdog"].get("poll_seconds", 20))
    log.write("watchdog_start", pid=os.getpid(), interval_seconds=interval,
              account=os.environ.get("USERNAME", "?"))
    try:
        while True:
            cycle(cfg, log, state)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.write("watchdog_stop", reason="keyboard interrupt")
        return 0


if __name__ == "__main__":
    sys.exit(main())
