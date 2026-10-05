# JARVIS

A personal agentic daemon for one Windows machine, built privacy first.

## What it is

JARVIS (`jarvisd`) is a resident Python daemon for Windows that reads a person's notes vault, active task, local git repositories, GitHub state and its own logs, and writes one morning digest note. Version 1 is observe-only: it never acts outward. Before anything is read into a payload for Claude, a deterministic tier gate decides what must stay on the machine, and the single batched `claude -p` call per digest runs with no tools, an isolated flag set and a daily budget cap. A local-model tier, a phone push, a read-only web page, a memory-consolidation pass and an `ask` command exist as adapters around that core. This README says which parts were run and which were only tested against fakes.

## Status

Phases follow the build order in the design (see `docs/v1-design.md`). The states are `done` (built, tested, and run for real by the author), `adapter only` (built and tested against a fake, never run against the real service or model) and `not built`.

| Phase | Name | State | What that means |
|---|---|---|---|
| 0 | Isolation | done | Kill switch, watchdog, hostile-daemon simulation and a sandbox policy (`bin/kill-switch.ps1`, `bin/watchdog.py`, `srt-settings.json`). Ran on the author's machine; the sandbox runtime could not be proven end to end without Administrator, and the v1 daemon is not under it. The kill switch also finds and kills the v1 daemon (`docs/killswitch-v1-patch.md`), proven against a stand-in daemon by the simulation (27 of 27 checks); a real trip against the live daemon has not been done. Record: `docs/phase0.md` |
| 1 | Inference base | adapter only | `jarvisd/local.py` talks to a local llama-server on loopback and is tested against a fake server. No model was downloaded, `bin/bench.ps1` was never run, so there is no throughput, latency or accuracy number. Details: `docs/local-tier.md` |
| 2 | Vertical slice | done | The morning digest end to end: queue, scheduler, tier gate, router contract, audit, CLI, toast. Run by hand on the author's machine since 2026-10-05, with a real Claude call. The unattended 06:30 scheduled run has not been observed yet (see "What this is not") |
| 3 | Claude bridge | done | The `claude -p` subprocess bridge with its budget ledger and breaker (`jarvisd/claude.py`) is used live. The Agent SDK escalation and claude-code-router are not built, by decision |
| 4 | Work hub | adapter only | The read-only web page (`jarvisd/hub/app.py`) and `jarvis ask` are built and tested; the page was not reviewed in a browser by its tests. GitHub, ClickUp and ntfy push are adapters tested against fakes. Not built: Slack, held-item triage, Inbox and Projects views, any action from the page |
| 5 | Consolidation | adapter only | `jarvisd/consolidate.py` proposes memory candidates from session notes. Off by default and never run against the real model. Details: `docs/consolidation.md` |
| 6 | Security widening | not built | No garak, promptfoo or mcp-scan run exists. Only the phase 0 kill-switch simulation is real |
| 7 | Retrieval | not built | Native search of the notes tool is used instead |
| 8 | Perception | not built | No code. Screenshots, OCR and browser automation are planned for rare cases only |
| 9 | Voice | not built | Gated on a Darija speech benchmark that does not exist yet |

## Architecture in 60 seconds

```
Task Scheduler "JarvisDaemon"  ->  jarvisd (one resident Python process)
   tick every 2 min + cron 06:30  ->  reconcile: is today's digest due?  ->  queue/ job file

   job -> 1 collect   notes vault | active task | git | GitHub | own logs     (read-only)
          2 gate 1    tier: path floor, tags, terms. Hit = held, file never opened
          3 router    (stub, or the local model) classifies what is left
          4 gate 2,3  importance, then confidence: where may this go?
          5 seal      clear_for_claude -> GatedPayload, the only type Claude accepts
          6 claude    ONE batched `claude -p`: no tools, no MCP, no settings, capped spend
          7 render    deterministic body, Claude's summary on top, held items listed
          8 write     the single vault writer: raw/jarvis/digest-<date>.md
          9 notify    toast, optional ntfy push (counts only)

   every step -> hash-chained audit log (ids, counts, hashes, never text)
   guards: state/KILL, pause, daily budget ledger, circuit breaker
   read-only views: jarvis CLI, jarvis hub (loopback web page)
```

More in `docs/architecture.md`.

## Privacy model

- **Tier gate before any read.** `tier.safe_read_text` is the only way collectors open files. A forbidden path is refused without opening it, and a text hit withholds the item. Held items reach the digest as opaque ids, never as content.
- **Fixed gate order.** Tier, then importance, then confidence. It is code, not a prompt and not a setting, and `dispatch.decide` is a pure function tested over its whole truth table.
- **Single writer for the vault.** One module writes into the notes vault, to a short list of allowed places, and an AST test fails if any other module writes a file or names a vault write path.
- **Isolated Claude argv.** Claude is started only from `jarvisd/claude.py`, with a list argv, no shell, an empty working directory, a scrubbed environment, `--tools ""` and `--strict-mcp-config`. Only a sealed `GatedPayload` is accepted, and the final prompt is scanned once more. One optional call is an exception, see below.
- **Audit chain.** Every action appends to a hash-chained JSONL log. `jarvis audit verify` walks it, and each digest carries the chain head so a later rewrite can be noticed.

**The one exception to the isolated argv is the optional ClickUp section, which is off by default (`clickup_enabled = false`).** Its call passes neither `--tools ""` nor `--strict-mcp-config`, because the ClickUp connector is not in a config file and has to stay visible. Every other connector and MCP server of the logged-in account is then visible to the model too and is only refused: by an allowlist of two read tools, a denylist that is a snapshot of the connector's tools on 2026-10-05, and `--permission-prompts none`. A tool the connector adds later is blocked by the permission mode alone. The call carries a constant prompt and no vault text, but its reply is untrusted input. Read the injection surface in `docs/v1-operations.md` before turning it on.

## Install and first run

You need Windows 11, Python 3.12 and git. `setup-venv.ps1` looks for the per-user python.org install, then for the `py` launcher; for any other install pass `-Python <path-to-python.exe>`. A logged-in Claude Code CLI is optional: without it every command still works and the digest is built deterministically. `gh` is optional for the GitHub section.

```powershell
git clone https://github.com/thelordofpigeons/jarvis.git
cd jarvis
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\setup-venv.ps1    # add -Python <path> if needed
copy jarvis.local.toml.example jarvis.local.toml
```

On another platform or without PowerShell, `pip install -r requirements.lock` in a Python 3.12 virtualenv gives the tested set, and `pip install ".[hub]"` adds the hub's dependencies to a plain install. Windows is the supported target.

Edit `jarvis.local.toml`. It is gitignored and is the only place your repository list, sensitive terms and private paths belong. The daemon expects a Markdown notes folder at `~/brain` (move it with `vault_write_raw` and `vault_write_sessions` under `[paths]`), laid out as described in `docs/vault-layout.md`: with no `RECENT.md` and no session notes the digest is empty, which is the right answer for an empty input. Then:

```powershell
.\jarvis.cmd self-test                  # every line PASS, or an explained SKIP
.\jarvis.cmd run-digest --dry-run       # prints the exact payload and the held list, writes nothing
.\jarvis.cmd run-digest                 # deterministic digest, no Claude
.\jarvis.cmd run-digest --claude --force   # the first paid call, capped by jarvis.toml (--force: today's digest already exists)
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\register-jarvisd-task.ps1 -WhatIf
.venv\Scripts\python.exe -m pytest -q   # the offline suite; it never calls Claude
.venv\Scripts\python.exe bin\watchdog.py --self-test   # the phase 0 config and dependency check
```

Read the dry-run output before the first `--claude` run: it is the exact text that would leave the machine. Drop `-WhatIf` to register the scheduled task. The full first-run checklist, stop and pause paths and the incident runbook are in `docs/v1-operations.md`.

## Commands

`jarvis` is `jarvis.cmd` in the repository root, or `python -m jarvisd`. Exit codes: 0 ok, 1 failure, 2 usage, 3 refused by a safety control.

| Command | What it does |
|---|---|
| `jarvis serve` | the resident loop; `--task` is task mode with Claude on, plain `serve` runs with Claude off |
| `jarvis run-digest` | build today's digest now; `--claude` allows the one paid call, `--dry-run` prints the payload only |
| `jarvis status` | daemon heartbeat, budget, queue and last digest |
| `jarvis digest` | print the latest digest, or its path |
| `jarvis held` | resolve a held id to its source and reason, in the terminal only |
| `jarvis wrong` | record a gate mistake; `--leak` opens the breaker |
| `jarvis pause` | stop enqueue and claim for a while |
| `jarvis resume` | clear the pause |
| `jarvis audit` | `tail`, `verify` the hash chain, or `cost` per day |
| `jarvis breaker` | `status`, or `reset` with a reason |
| `jarvis local` | `status` and `check` of the local model tier; downloads nothing |
| `jarvis hub` | the read-only web page on loopback; `--check` renders every view |
| `jarvis ask` | answer one question from the latest digest and recent notes, one capped call |
| `jarvis consolidate` | propose memory candidates from session notes (off by default) |
| `jarvis clickup` | `check` the flag set for the optional ClickUp section; `--live` makes one paid call |
| `jarvis self-test` | PASS, FAIL and SKIP checks; exit 1 on any FAIL |
| `jarvis install-task` | print or run the Task Scheduler registration |

## Cost

One real digest call is recorded in `docs/first-run-log.md`: 0.0068 USD for a small payload (4 items to Claude, 961 cache-creation input tokens, 295 output tokens). It is one measurement, not a range; a larger payload costs more, and `jarvis audit cost --days 7` sums what your own runs settled. Three limits apply before any call is spawned: a per-call cap (`max_budget_usd`, 0.50), a daily ledger (`daily_budget_usd`, 2.00, at most 6 calls) and a circuit breaker that opens after repeated failures or a rate limit and stays open until it clears or, after a privacy event, until a human resets it. `jarvis audit cost --days 7` sums what was actually settled. The deterministic digest costs nothing.

## What this is not

- **Not sandboxed.** The v1 daemon runs as the human account, because the Claude login, the repositories and the toast all live there. The Phase 0 sandbox and account isolation do not constrain it. The controls are in code (the tier gate, the sealed payload, one writer, the ledger and breaker), not in an operating-system boundary.
- **Tamper-evident, not tamper-proof.** The audit chain detects edits, but a process under the same account could rewrite the log and its chain together. The chain head stored in each digest is the out-of-band check, and tail truncation is only detectable that way.
- **No local model has been benchmarked.** The local tier is an adapter exercised against a fake server. There is no measured speed or accuracy, and the privacy model for untagged personal text is heuristic, so read the dry run and keep the sensitive-terms list current.
- **Not proven unattended.** Every digest so far was started by hand or by a manual `schtasks /run`. The 06:30 scheduled path (trigger, catch-up, the wait for a fresh `RECENT.md`) is tested against a fake clock, but a scheduled run with nobody at the keyboard has not been observed. Check `jarvis status` the morning after you register the task.
- **Not 24/7.** It is resident while the machine is awake and the user is logged on. A sleep through 06:30 gives a late digest.
- **Not a finished product.** It is one person's tool, written for one person's vault layout. Expect to adapt the collectors.

## Roadmap

1. Run a real model through the audit steps and the benchmark script, then decide on the hardware question the design leaves open.
2. A held-item triage job, so sensitive items are summarized locally instead of only held.
3. Validate the optional parts against the real services (ClickUp connector, ntfy server) and look at the hub page in a browser.
4. Trip the elevated kill switch once against the live daemon, at the machine, and recover from it (`docs/killswitch-v1-patch.md`, "How to verify"). The simulation passes; the real path has not been exercised.
5. Later phases only where they earn their place: security tooling, retrieval, perception, voice.

## Documentation

- `docs/architecture.md`: processes, layers, modules, gates and the Claude call.
- `docs/v1-operations.md`: the operator manual: start, stop, pause, audit, incident runbook, known gaps.
- `docs/first-run-log.md`: what the first real run showed.
- `docs/vault-layout.md`: what the notes collector reads, what it writes, and what a fresh install shows.
- `docs/publishing.md`: the rules for publishing the repository, including its history.
- `docs/v1-design.md` and `docs/v1-plan.md`: the full design with its decisions and the build plan.
- `docs/phase0.md`, `docs/phase0-runbook.md` and `docs/sandbox-policy.md`: isolation, the kill switch and the sandbox policy.
- `docs/killswitch-v1-patch.md`: how the kill switch was extended to the v1 daemon, what it does step by step, and how to verify it.
- `docs/local-tier.md`, `docs/hub.md`, `docs/consolidation.md` and `docs/notify-ntfy.md`: the optional parts, each with its status on top.
- `CHANGELOG.md`, `CONTRIBUTING.md` and `CITATION.cff`.

## License

MIT, see `LICENSE`.
