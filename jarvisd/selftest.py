"""`jarvis self-test`: PASS/FAIL lines in the style of `bin/watchdog.py --self-test`.

Layer L4. Each check is small, offline and free: nothing here calls a paid model. The vault
checks run against a throwaway tree under the system temp folder, never the real vault.
A check that raises is reported as a FAIL with the exception class, never as a crash.

Limits, stated plainly:
- `--live` is accepted but the paid smoke belongs to task T12 (tests/live/); this build only
  prints a SKIP line for it and spends nothing.
- A scheduled task that is not registered yet is a SKIP, not a FAIL, because a development
  checkout legitimately has none. A task that is registered but Disabled is a FAIL.
"""
from __future__ import annotations

import os
import re
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from jarvisd import ROOT
from jarvisd.audit import AuditLog
from jarvisd.claude import SYSTEM_PROMPT, ClaudeClient, build_argv
from jarvisd.config import SENSITIVE_FLOOR_GLOBS, Config, build_config, load_config
from jarvisd.daemon import TASK_NAME, audit_path, query_task_state, status_is_disabled
from jarvisd.dispatch import decide
from jarvisd.fsio import atomic_write_text
from jarvisd.jobstore import JobStore
from jarvisd.models import RouterDecision, TierHit
from jarvisd.notify import HOOKS_SCRIPT
from jarvisd.state import StateStore
from jarvisd.tier import path_hit, text_hit
from jarvisd.vault import VaultWriteDenied, VaultWriter

Outcome = tuple[bool | None, str]  # True pass, False fail, None skip
GENERATED = "---\ntype: selftest\ngenerator: jarvisd\n---\nselftest\n"
ALIGNMENT = ("kill switch finds the v1 daemon: task name and command line marker match the registration; "
             "it stops and disables the task, writes state/KILL and kills the heartbeat pid's process tree "
             "(hostile simulation: tests/hostile-sim.ps1)")
KILL_SWITCH_SCRIPT = ROOT / "bin" / "kill-switch.ps1"


class _NullAudit:
    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        return {}


# --- individual checks ---------------------------------------------------------------------------


def check_config(cfg: Config) -> Outcome:
    base = load_config(ROOT / "jarvis.toml", use_local=False)
    if not base.sha256 or not cfg.sha256:
        return False, "config has no hash"
    with tempfile.TemporaryDirectory(prefix="jarvis-selftest-") as tmp:
        local = Path(tmp) / "jarvis.local.toml"
        atomic_write_text(local, '[gates]\nsensitive_terms = ["selftest-only-term"]\n')
        merged = load_config(ROOT / "jarvis.toml", local_path=local)
    if "selftest-only-term" not in merged.gates.sensitive_terms:
        return False, "the local override did not merge"
    if any(tag not in merged.gates.sensitive_tags for tag in base.gates.sensitive_tags):
        return False, "the local override removed a tracked sensitive tag"
    return True, f"sha256 {cfg.sha256[:12]}, local override merges and cannot remove entries"


def check_floor(cfg: Config) -> Outcome:
    globs = cfg.sensitive_globs()
    missing = [g for g in SENSITIVE_FLOOR_GLOBS if g not in globs]
    if missing:
        return False, f"floor globs missing: {missing}"
    return True, f"{len(SENSITIVE_FLOOR_GLOBS)} floor globs, {len(globs)} total"


def check_dirs(cfg: Config) -> Outcome:
    """Runtime folders are created on demand (they are gitignored); deploy/ ships with the repo."""
    for runtime in (cfg.paths.queue, cfg.paths.logs, cfg.daemon.state_dir):
        runtime.mkdir(parents=True, exist_ok=True)
    wanted = {"queue": cfg.paths.queue, "logs": cfg.paths.logs, "state": cfg.daemon.state_dir,
              "deploy": cfg.paths.root / "deploy"}
    missing = [name for name, path in wanted.items() if not path.is_dir()]
    return (False, f"missing: {', '.join(missing)}") if missing else (True, ", ".join(wanted))


def check_queue_location(cfg: Config) -> Outcome:
    JobStore.from_config(cfg)  # raises UnsafeRoot under brain/, inside the vault or in a Syncthing folder
    return True, str(cfg.paths.queue)


def _temp_vault_config(root: Path) -> Config:
    """The tracked config with every path moved under `root`, built from plain dict keys."""
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    brain = root / "brain"
    for rel in ("telos", "notes", "raw/jarvis", "sessions"):
        (brain / rel).mkdir(parents=True, exist_ok=True)
    raw["paths"].update({
        "root": (root / "jarvis").as_posix(), "queue": (root / "jarvis" / "queue").as_posix(),
        "logs": (root / "jarvis" / "logs").as_posix(), "models": (root / "jarvis" / "models").as_posix(),
        "vault_write_raw": (brain / "raw" / "jarvis").as_posix(),
        "vault_write_sessions": (brain / "sessions").as_posix(),
        "vault_forbidden": [(brain / "telos").as_posix(), (brain / "notes").as_posix(),
                            (root / "Documents" / "Work").as_posix()],
    })
    raw["digest"]["write_session_note"] = False
    return build_config(raw)


def check_vault_denies(cfg: Config) -> Outcome:
    with tempfile.TemporaryDirectory(prefix="jarvis-selftest-") as tmp:
        tcfg = _temp_vault_config(Path(tmp))
        writer = VaultWriter(tcfg, _NullAudit())  # type: ignore[arg-type]
        denied = {
            "telos target": lambda: writer.write_raw("../telos/x.md", GENERATED, "selftest"),
            "telos folder": lambda: writer.write_raw("telos/x.md", GENERATED, "selftest"),
            "notes folder": lambda: writer.write_raw("notes/x.md", GENERATED, "selftest"),
            "Documents/Work": lambda: writer.write_raw("Documents/Work/x.md", GENERATED, "selftest"),
            "absolute path": lambda: writer.write_raw("C:/Documents/Work/x.md", GENERATED, "selftest"),
            "not markdown": lambda: writer.write_raw("x.txt", GENERATED, "selftest"),
            "no generator marker": lambda: writer.write_raw("plain.md", "no marker\n", "selftest"),
            "non-jarvis session": lambda: writer.write_session("Not A Slug", GENERATED, "selftest"),
            "session notes off": lambda: writer.write_session("telos", GENERATED, "selftest"),
        }
        for label, attempt in denied.items():
            try:
                attempt()
            except VaultWriteDenied:
                continue
            return False, f"the writer accepted: {label}"
        written = writer.write_raw("digest-selftest.md", GENERATED, "selftest")
        if not written.path.is_file():
            return False, "the allowed write did not land"
    return True, f"{len(denied)} refusals, one allowed write, all in a temp tree"


def check_tier_fixtures(cfg: Config) -> Outcome:
    hits = {
        "telos path": path_hit("C:/synthetic/brain/telos/identity.md", cfg),
        "notes path": path_hit("C:/synthetic/brain/notes/a.md", cfg),
        "sensitive component": path_hit("C:/synthetic/work/sensitive/a.md", cfg),
        "sensitive flag": text_hit("title: x\nsensitive: true\n", cfg),
    }
    if cfg.gates.sensitive_tags:
        hits["frontmatter tag"] = text_hit(f"---\ntags: [{cfg.gates.sensitive_tags[0]}]\n---\nbody\n", cfg)
    misses = [label for label, hit in hits.items() if hit is None]
    if misses:
        return False, f"no hit for: {', '.join(misses)}"
    if path_hit("C:/synthetic/projects/readme.md", cfg) is not None:
        return False, "a benign path was held"
    return True, f"{len(hits)} fixtures hit, one benign path passes"


def _decision(**over: Any) -> RouterDecision:
    base: dict[str, Any] = {"category": "brain_thread", "sensitive": False, "importance": "low",
                            "confidence": 0.99, "needs_tools": [], "language": "en", "reason": "selftest"}
    return RouterDecision(**{**base, **over})


def check_gate_order(cfg: Config) -> Outcome:
    from jarvisd.models import Item

    item = Item(id="selftest", source="brain", kind="brain_thread", title="t")
    sensitive = TierHit(kind="sensitive", code="selftest", where="path")
    policy = TierHit(kind="policy", code="selftest", where="item")
    rows: list[tuple[str, Callable[[], Any], tuple[str, str]]] = [
        ("tier hit held", lambda: decide(item, sensitive, None, cfg, "not_installed"), ("held", "tier")),
        ("tier hit local when up", lambda: decide(item, sensitive, None, cfg, "up"), ("local", "tier")),
        ("policy hit held", lambda: decide(item, policy, None, cfg, "up"), ("held", "tier")),
        ("router sensitive held", lambda: decide(item, None, _decision(sensitive=True), cfg, "up"), ("held", "tier")),
        ("importance high", lambda: decide(item, None, _decision(importance="high"), cfg, "up"),
         ("claude", "importance")),
        ("escalate category", lambda: decide(item, None, _decision(category=cfg.gates.importance_escalate[0]),
                                             cfg, "up"), ("claude", "importance")),
        ("low confidence", lambda: decide(item, None, _decision(confidence=0.1), cfg, "up"),
         ("claude", "confidence")),
        ("passes, no local", lambda: decide(item, None, _decision(), cfg, "not_installed"),
         ("claude", "local_absent")),
        ("passes, local up", lambda: decide(item, None, _decision(), cfg, "up"), ("local", "none")),
    ]
    for label, call, (route, by) in rows:
        got = call()
        if (got.route, got.decided_by) != (route, by):
            return False, f"{label}: got {got.route}/{got.decided_by}, expected {route}/{by}"
    if decide(item, None, _decision(), cfg, "unavailable").degraded is not True:
        return False, "an unavailable local tier did not mark the result degraded"
    return True, f"{len(rows) + 1} rows hold, tier before importance before confidence"


def check_argv_golden(cfg: Config) -> Outcome:
    golden = [
        "claude", "-p", "--output-format", "json", "--model", cfg.claude.model or "sonnet",
        "--setting-sources", "", "--disable-slash-commands", "--strict-mcp-config", "--tools", "",
        "--no-session-persistence", "--permission-prompts", "none",
        "--max-budget-usd", f"{cfg.claude.max_budget_usd:g}", "--system-prompt", SYSTEM_PROMPT,
    ]
    got = build_argv(cfg, SYSTEM_PROMPT, binary="claude")
    return (True, "matches design section 7, empty strings kept") if got == golden else (False, "argv drifted")


def check_toast(cfg: Config) -> Outcome:
    if cfg.notify.toast_script.is_file():
        return True, str(cfg.notify.toast_script)
    if HOOKS_SCRIPT.is_file():
        return True, f"deploy script missing, falling back to {HOOKS_SCRIPT}"
    return False, f"neither {cfg.notify.toast_script} nor {HOOKS_SCRIPT} exists"


def _claude_optional() -> bool:
    """CI runners (GitHub sets CI=true) have no Claude login; a stranger can set JARVIS_NO_CLAUDE=1."""
    return any(os.environ.get(name, "").strip().lower() not in ("", "0", "false") for name in ("CI", "JARVIS_NO_CLAUDE"))


def check_claude(cfg: Config, runner: Any) -> Outcome:
    client = ClaudeClient(cfg, _NullAudit(), StateStore.from_config(cfg), runner=runner, enabled=False)  # type: ignore[arg-type]
    report = client.preflight()
    if not report.ok and report.reason == "binary_not_found" and _claude_optional():
        # Only "not installed" is skippable. An installed CLI that lacks a flag still FAILS.
        return None, "no claude binary here and none is expected (CI or JARVIS_NO_CLAUDE is set)"
    if not report.ok:
        extra = f" ({', '.join(report.missing_flags)})" if report.missing_flags else ""
        return False, f"{report.reason}{extra}"
    return True, f"{report.version}, all {len(cfg.claude.required_flags)} required flags advertised"


def check_task(command_runner: Any) -> Outcome:
    status = query_task_state(command_runner)
    if status is None:
        return None, f"{TASK_NAME} is not registered (run jarvis install-task)"
    if status_is_disabled(status):
        return False, f"{TASK_NAME} is Disabled (the kill switch or a hand edit)"
    return True, f"{TASK_NAME} is {status}"


def check_audit_chain(cfg: Config) -> Outcome:
    log = AuditLog(audit_path(cfg), mirror_stdout=False)
    if not log.path.exists():
        return True, "no audit records yet"
    ok, bad = log.verify([log.path])
    return (True, "live chain verifies") if ok else (False, f"chain broken at seq {bad}")


def _ps_default(src: str, name: str) -> str | None:
    """The single-quoted default of a `[string] $Name = '...'` parameter in a PowerShell script."""
    match = re.search(rf"\[string\]\s*\${name}\s*=\s*'([^']*)'", src)
    return match.group(1) if match else None


def check_alignment(script: Path = KILL_SWITCH_SCRIPT) -> Outcome:
    """The kill switch must look for the daemon the way the daemon is actually registered.

    Reads bin/kill-switch.ps1 and compares its defaults with the task name the daemon checks and
    the command line `jarvis install-task` registers. A rename on either side that is not
    mirrored on the other would leave the kill switch blind, and nothing else would notice.
    """
    from jarvisd.cli import registration_block  # late: cli imports this module

    if not script.is_file():
        return False, f"{script} not found, the kill switch is missing"
    src = script.read_text(encoding="utf-8")
    problems: list[str] = []
    task = _ps_default(src, "TaskName")
    if task != TASK_NAME:
        problems.append(f"task name {task!r} in the script, {TASK_NAME!r} in the daemon")
    marker = _ps_default(src, "V1Marker")
    if not marker or marker not in registration_block():
        problems.append(f"marker {marker!r} is not on the command line that install-task registers")
    for step in ("heartbeat.json", "'create-kill-file'", "'stop-task'", "'kill-daemon-tree'"):
        if step not in src:
            problems.append(f"step {step} is missing")
    if problems:
        return False, "; ".join(problems)
    return True, ALIGNMENT


# --- runner --------------------------------------------------------------------------------------------


def run(cfg: Config, live: bool = False, *, runner: Any = None, command_runner: Any = None,
        out: Callable[[str], None] = print) -> int:
    """Run every check, print one line each, return 1 if any FAILED else 0."""
    checks: Sequence[tuple[str, Callable[[], Outcome]]] = [
        ("config-loads", lambda: check_config(cfg)),
        ("floor-present", lambda: check_floor(cfg)),
        ("required-dirs", lambda: check_dirs(cfg)),
        ("queue-location", lambda: check_queue_location(cfg)),
        ("vault-writer-denies", lambda: check_vault_denies(cfg)),
        ("tier-fixtures-hit", lambda: check_tier_fixtures(cfg)),
        ("gate-order-truth-table", lambda: check_gate_order(cfg)),
        ("build-argv-golden", lambda: check_argv_golden(cfg)),
        ("toast-script-exists", lambda: check_toast(cfg)),
        ("claude-resolves", lambda: check_claude(cfg, runner)),
        ("daemon-task-state", lambda: check_task(command_runner)),
        ("audit-chain", lambda: check_audit_chain(cfg)),
        ("killswitch-alignment", check_alignment),
    ]
    failed = skipped = ran = 0
    for name, check in checks:
        try:
            ok, detail = check()
        except Exception as exc:  # noqa: BLE001  a broken check is a FAIL, not a crash
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:120]}"
        if ok is None:
            skipped += 1
            out(f"[SKIP] {name} {detail}".rstrip())
            continue
        ran += 1
        failed += 0 if ok else 1
        out(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())
    if live:
        skipped += 1
        out("[SKIP] live-smoke the paid smoke is not part of this build (task T12, tests/live/); nothing was spent")
    counted = ran
    out(f"\nself-test: {counted - failed}/{counted} checks passed" + (f", {skipped} skipped" if skipped else ""))
    return 1 if failed else 0
