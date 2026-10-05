# Phase 0: isolation, kill switch and sandbox

Phase 0 of the build order (see the status table in the README) prepares a machine to host a
local-model daemon safely: a separate Windows account, a kill switch, a watchdog and a sandbox
policy. It is the author's own setup record, kept here because the scripts in `bin/` and the
policy in `srt-settings.json` come from it.

How to read it:

- It is a log of one machine. The `jarvis` account, the scheduled tasks and the firewall rule it
  mentions were created by `bin/phase0-elevated.ps1` on that machine and do not exist on yours.
- The steps to reproduce it are in `docs/phase0-runbook.md`. The sandbox policy and its open
  blocker are in `docs/sandbox-policy.md`.
- The v1 digest daemon does not run under the `jarvis` account and is not covered by the sandbox
  or the firewall rule. Why, and what covers it instead, is in `docs/architecture.md` and the
  kill-switch extension for it in `docs/killswitch-v1-patch.md` (applied, simulated).
- Paths such as `~/brain/raw/...` point into the author's private notes vault. Nothing in this
  repository needs it.

The text after the marker is the original Phase 0 narrative, unchanged. A test pins it, so a
deliberate edit has to update the hash in `tests/test_docs.py`.

<!-- phase0-record:begin -->
Local-model daemon and work hub for one personal machine (`<machine>`). Personal
JARVIS component, not a work-tracked task, so no ticket gating.

- **Plan, source of truth:** `~/brain/raw/2026-09-17-local-models-sota-research.md` v2, sections 1, 4, 9, 13
- **Adjudication:** the `-review.md` sibling
- **Program anchor:** `~/brain/projects/jarvis/README.md`
- **Config:** `jarvis.toml`, human-owned, the daemon reads it and never writes it

Build started 2026-09-22. Phase 0 of 9.

## Phase 0 state

Done and verified:

| Piece | Where | Verification |
|---|---|---|
| Project scaffold and config contract | `jarvis.toml` | watchdog self-test, 15 of 15 |
| Kill switch | `bin/kill-switch.ps1` | hostile simulation, 7 of 7, Daemon scope |
| Hostile-daemon simulation | `tests/hostile-sim.ps1` | caught a self-termination bug, now fixed |
| Watchdog: health probe, GPU-yield sensor, kill-switch trigger | `bin/watchdog.py` | self-test plus one live cycle |
| Phase 1 benchmark wrapper | `bin/bench.ps1` | prerequisite guard returns exit 4 |
| sandbox-runtime installed, version 0.0.77 | npm global | CLI runs, version confirmed |
| Sandbox policy for daemon actions | `srt-settings.json` | valid JSON, schema-safe |

**Elevated setup ran 2026-09-22, 25 steps, zero failures.** Landed: the `jarvis`
account, the vault ACL grants and denials, TdrDelay and TdrDdiDelay at 60, driver
pinning, the `JarvisKillSwitch` task, and the egress block rule with its SDDL
verified against the jarvis SID.

The kill switch is now verified on both branches: unelevated Daemon scope by the
hostile simulation, and the elevated branch through the real production path
(watchdog to scheduled task to firewall), which returned exit 0 and enabled the
rule.

Still open, all needing Administrator:

1. **Disable the tripped egress rule.** The elevated verification left it enabled.
   `Disable-NetFirewallRule -DisplayName 'JARVIS-daemon-egress-block'`
2. **Add jarvis to Users.** `New-LocalUser` joins no group at all, so the account
   currently cannot log on locally. Fixed in the script for future runs.
   `Add-LocalGroupMember -Group Users -Member jarvis`
3. **Reboot**, so TdrDelay takes effect.
4. **Credential Manager entries**, which must be added while signed in as jarvis.
5. **Full-scope kill-switch test**, at the machine only.

Items 1 and 2 were done 2026-09-22, along with the account password change and the
sandbox machine install.

**Open blocker, carried into phase 2:** sandbox-runtime installed and provisioned,
but a sandboxed probe fails from a non-elevated shell because srt cannot verify its
own egress fence without Administrator. It fails closed rather than running
unsandboxed, which is the right behavior, but it means "srt proven end to end"
remains unmet and the daemon may not be able to wrap its own actions while running
unprivileged. Diagnosis and the one-command test: `docs/sandbox-policy.md`.

## Commands

```powershell
python bin\watchdog.py --self-test       # config and dependency check
python bin\watchdog.py --once            # single probe cycle
python bin\watchdog.py                   # resident loop
python bin\watchdog.py --trip "reason"   # trip the kill switch
powershell -File tests\hostile-sim.ps1   # re-verify the kill switch
powershell -File bin\bench.ps1 -ModelPath models\<model>.gguf   # phase 1
```

## Design notes worth not relearning

- **The kill switch excludes itself and its ancestors.** The marker it is given
  appears on its own command line, so without that exclusion it kills itself
  mid-run and never writes the audit line. The hostile simulation found this.
- **Daemon scope against Full scope.** Daemon stops the daemon and blocks its
  egress and is safe to trip remotely. Full also drops RustDesk, sshd and Tailscale,
  which severs your own access, so it is never run by a test and never over SSH.
- **`absent` is not a failure.** The health probe distinguishes a refused
  connection from an unhealthy response, so phase 0 with no server running does not
  look like a crash loop.
- **Restart budget.** Four restarts per rolling hour, then it stops trying and
  notifies. A watchdog that restarts forever hides the fault it exists to surface.
- **GPU yield is presence-based only.** VRAM headroom is not readable on AMD under
  Windows without a vendor tool, so `gpu_yield_vram_headroom_mb` is declared but not
  enforced. Stated rather than pretended.
- **No secrets in this repo.** Values live in Credential Manager under the jarvis
  account; `jarvis.toml` holds entry names only.
- **Never call `srt` by name.** Python's subtitle tool installs a script with the
  same name earlier on PATH, so a bare `srt` runs the subtitle parser and exits
  cleanly while sandboxing nothing. The absolute path is pinned in `jarvis.toml`.
- **Two isolation layers.** `jarvis` is the account the daemon runs as, governed by
  NTFS ACLs. `srt-sandbox` is a separate account each sandboxed child runs as,
  governed by `srt-settings.json`. See `docs/sandbox-policy.md`.
- **The sandbox policy file cannot carry comments.** Its schema is strict and an
  invalid file is a hard error rather than a fallback to defaults, so the rationale
  lives in `docs/sandbox-policy.md` instead.

## Open decisions

- Should the daemon read `brain/telos` non-sensitive tiers? Recommended yes, with
  the `telos/sensitive` denial in place. See runbook step 2.
- Agent model: gpt-oss-20b or Qwen3-30B-A3B, decided from phase 1 benchmarks.
- Phase 4 surface, flagged in the plan for confirmation at build start: Slack
  through the Claude.ai connector inside scheduled `claude -p` runs first with a
  user-token app later, web GUI over `tailscale serve` rather than a TUI, CLI keeps
  the quick actions.

## Constraint

C: has 58 GB free and is the only drive. Pull gpt-oss-20b first, bench it, and only
pull Qwen3-30B-A3B if the benchmark leaves the decision genuinely open.
