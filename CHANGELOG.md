# Changelog

All notable changes to this project. Dates are the day the work was verified on the author's
machine. The format follows Keep a Changelog; versions are not published to PyPI.

## Unreleased

The work hub moves from a viewer to a cockpit that can turn a proposal into a task, and only on a
click. The version number is chosen at release (planned: 1.2.0). Each entry says what was run.
Nothing below spent money: no paid proposals run was made and ClickUp was never called. The hub
is no longer GET only: the Inbox adds three POST routes.

### Added: built and tested offline
- Q1, task proposals (`jarvis propose`, `jarvis proposals`, `jarvisd/propose.py`, `docs/proposals.md`):
  one capped Claude call turns the latest complete digest run's cleared items into at most
  `[propose].max_proposals` proposals under `state/proposals/`. Same gates, sealed payload and
  isolated argv as the digest; held items appear as ids only; every evidence id is checked against
  what was sent; the 20 newest rejected proposals are sent back as negative examples. Off by
  default. Only the dry run was ever executed against real state.
- Q2, tracker adapters (`jarvis tracker check`, `jarvisd/tracker.py`): a `markdown` adapter that
  appends to `raw/jarvis/confirmed-tasks.md` through the single vault writer, and a `clickup`
  adapter that creates one task through the ClickUp REST API with a token read from a named
  environment variable. A `dry_run` switch logs the exact request instead of sending it, and
  every attempt is audited. The ClickUp adapter has only met a local stand-in server.
- Q3, Projects and Ledger views in the hub: one row per configured repository with fixed-rule
  risks, and a record of what was delivered, built from the digest notes, the proposals and the
  run manifests. They run no git and ask no model. New keys `[hub].stale_days` and
  `[hub.task_projects]`.
- Q4, the Inbox (`jarvisd/inbox.py`, `jarvisd/hub/inbox.py`, `tests/test_inbox.py`): confirm,
  edit then confirm, and reject, from the hub and from `jarvis proposals confirm|reject`, through one
  implementation. The first write actions in the hub: three POST routes behind a per-process CSRF
  token and an Origin and Host check, a decision is made once under a cross-process lock, the
  tracker is asked before the file is written, a reject needs a reason, and every write is audited
  by id only. Tested with a fake tracker; no real proposal has been decided.
- Q5, documentation: `docs/proposals.md`, an updated `docs/hub.md`, and the README status row and
  command table. `jarvis.local.toml.example` carries synthetic `[tracker]` entries.

### Changed
- `[propose]` and `[tracker]` tables are appended to `jarvis.toml` (both adapters and the job are
  inert by default). `models.Proposal.tracker_ref` accepts a `file:` link as well as http and https.

## 1.1.0 - 2026-10-05

The first public release. Every entry says whether the feature is in use, is an adapter or feature
tested only against a fake service, binary or model, or is only designed. The README status table says
the same per build phase. The suite has 1292 offline tests (3 more are skipped or live-only) and spends nothing.

### Added: built, tested and in use
- Publishing files: `LICENSE` (MIT), `CITATION.cff`, this changelog, `.gitattributes` (LF
  everywhere), a GitHub Actions workflow that runs the offline suite on Windows with Python 3.12
  (no Claude binary, login or secret on the runner), `docs/vault-layout.md` and
  `docs/publishing.md`.
- Hygiene tests for a public repository: user-profile paths, owner and machine names, Tailscale
  addresses, e-mail addresses, token shapes, a hashed list of names that must not be spelled in a
  file, a reasoned allowlist, and a scan of the git history (author and committer addresses,
  commit messages and every line ever added).
- A `hub` extra in `pyproject.toml` (`pip install ".[hub]"`), and `deploy/setup-venv.ps1` finds
  Python 3.12 through the `py` launcher or a `-Python` path.
- `[digest].watched_tasks`: the scheduled tasks whose last run and result appear under "what
  JARVIS did while you slept" (default: the daemon's own task).

### Added: adapters, tested against fakes, never run against the real service or model
- A read-only GitHub section in the digest (`[digest.github]`, through the `gh` command line:
  open and review-requested pull requests, the latest CI status, stale branches) with the same
  tier gate as git items. Enabled in the tracked `jarvis.toml`; set `enabled = false` under
  `[digest.github]` to turn it off, or leave `gh` uninstalled and it reports itself unavailable.
- `jarvis ask "<question>"`: one question answered from the latest digest, recent notes and open
  threads, through the same gates, one capped call, ids checked against what was sent.
  `--dry-run` prints the payload and writes nothing, not even audit records.
- The local model tier (`jarvisd/local.py`, `docs/local-tier.md`): an OpenAI-compatible client for
  a llama-server on loopback, a local router and backend behind the stricter local trust
  allowlist, a four-state tier machine. No model was downloaded and no benchmark was run, so
  there is no speed or accuracy number.
- The read-only work hub (`jarvis hub`, `jarvisd/hub/`, `docs/hub.md`): a loopback web page over
  the digest, the queue and the audit. Its tests do not look at it in a browser.
- Memory consolidation (`jarvis consolidate`, `docs/consolidation.md`): proposes candidates from
  session notes. Off by default.
- ntfy push (`docs/notify-ntfy.md`): counts and a note link only, optional bearer token read from a
  named environment variable.
- The optional ClickUp section (`jarvis clickup check`, `docs/v1-operations.md`): a second
  isolated call with a read-only tool allowlist. Off by default.

### Changed
- The tracked `jarvis.toml` is machine-agnostic: `~` is the user's home, other relative paths are
  relative to the repository root, and the machine and account names are placeholders. Real
  values live in the gitignored `jarvis.local.toml`, which may also add folders to
  `[paths].vault_forbidden`.
- The repository flag, the metadata switch and the importance bucket no longer carry the name of
  the author's employer: they are now `work`, `work_metadata_to_claude` and `work_prod`, and the
  digest prints `(work)` after such repositories. A `jarvis.local.toml` written for 1.0 keeps
  loading, because the old spellings are rewritten on load; rename them when you next edit it.
  Both spellings in one table is an error. The sandbox policy (`srt-settings.json`) now names a
  placeholder `~/Documents/Work`; add your own private folder to your copy.
- ntfy: `ntfy_url` must be `https://`, except for a loopback host. A bearer token and the digest
  line no longer cross a LAN in clear text.
- `bin/watchdog.py --self-test` creates the gitignored runtime folders instead of failing on a
  fresh clone.
- README rewritten for a public audience: what it is, a status table per phase with honest
  states, a 60 second architecture, the privacy model (including the ClickUp exception to the
  isolated argv), install and first run, the CLI table, cost, and a "what this is not" section.
  The Phase 0 narrative moved unchanged to `docs/phase0.md`, the first-run diary to
  `docs/first-run-log.md`. Added `docs/architecture.md` and `CONTRIBUTING.md`.
- Docs, scripts and fixtures no longer name the author's account, machine, employer, clients or
  private scheduled tasks; the design's open-questions section no longer carries private context.
- Versions agree everywhere: package, `jarvis.toml [meta]`, citation and changelog are 1.1.0.

### Fixed
- Every command now writes UTF-8 to a redirected or piped stdout. A note containing an arrow or an
  emoji used to crash `ask --dry-run`, `run-digest --dry-run` and `consolidate --dry-run` under a
  cp1252 pipe, and `ask` after the paid call had been made.
- `ask --dry-run` no longer appends `gate_decision` records to the audit chain, which later asks
  read as "latest decision wins".
- A test that failed every day between 20:00 and 24:00 UTC: the digest end-to-end rig recomputed
  its date from an advanced clock.
- The README first-run sequence ran `run-digest --claude` without `--force`, which is refused once
  today's digest exists; it also activated the venv with a script the default PowerShell policy
  blocks.

### Known gaps
- The 06:30 scheduled run has not been observed unattended. Every digest so far was started by
  hand or by `schtasks /run`.
- The ClickUp call is not under `--tools ""` and `--strict-mcp-config`; see the README privacy
  model.
- Not sandboxed: the Phase 0 `srt` sandbox and NTFS denials do not constrain this daemon.
- No garak, promptfoo or mcp-scan run exists; retrieval, perception and voice are not built.
- The elevated kill switch has been simulated against a stand-in daemon, not tripped for real.

## 1.0.0-opt2 - 2026-10-05

The v1 digest daemon ("option 2"): observe-only, one batched Claude call a day, local model
tier stubbed. Design in `docs/v1-design.md`, operations in `docs/v1-operations.md`.

### Added
- `jarvisd`, a resident daemon run as a Windows scheduled task (`JarvisDaemon`), with a
  hash-chained audit log, a durable job queue, a state store, a scheduler and a `jarvis` CLI.
- A fail-closed tier gate that runs before anything is read into a Claude-bound payload, a
  stub router behind a replaceable adapter, and a dispatch layer with the fixed gate order
  (tier, importance, confidence).
- A `claude -p` wrapper with an isolated argv, a daily budget ledger, a circuit breaker and a
  sealed payload type; tests run it against a fake binary and never spend money.
- Collectors for the notes vault, the active task, local git repositories (an explicit
  allowlist) and the daemon's own logs, and a deterministic digest renderer.
- A single vault writer, enforced by an AST test, that may only write the digest note.
- Toast notification, retention, kill paths (`state/KILL`, task stop, self-exit when the task
  is disabled) and `jarvis self-test`.
- 734 offline tests, two live-only tests behind `-m live`.

### Known gaps
- Not sandboxed: the Phase 0 `srt` sandbox and NTFS denials do not constrain this daemon.
- The local model tier is not installed; sensitive items are held, not routed local.
- The audit chain is tamper-evident, not tamper-proof.
- The kill switch's process-kill step did not match the owner's own account at the time of this
  release; it does now (`docs/killswitch-v1-patch.md`, simulated, no real trip yet).

## 0.1.0 - 2026-09-22

Phase 0: the safety scaffolding, before any model or daemon exists.

### Added
- `jarvis.toml`, the human-owned configuration contract, and `bin/watchdog.py` (health probe,
  GPU-yield sensor, restart budget, kill-switch trigger) with a 15 check self-test.
- `bin/kill-switch.ps1` and `tests/hostile-sim.ps1`, a hostile-daemon simulation that found and
  fixed a self-termination bug.
- `bin/phase0-elevated.ps1`, the idempotent elevated setup (restricted account, vault ACLs,
  TDR settings, egress block rule, kill-switch scheduled task).
- `bin/bench.ps1`, a phase 1 benchmark wrapper, and the sandbox policy (`srt-settings.json`,
  `docs/sandbox-policy.md`).
- `docs/phase0-runbook.md`.

### Known gaps
- Sandboxed probes fail closed from a non-elevated shell, so "sandbox proven end to end" is
  still unmet.
- GPU yield is presence-based; the VRAM headroom setting is declared but not enforced.
