# Architecture

How `jarvisd` is put together and why, in the order a new reader needs it: the processes and
layers, the modules, the job and state files, the gate semantics and the one place Claude is
called. This page is a condensed and updated version of sections 3 to 7 of `docs/v1-design.md`.
That file has the full rationale, the judge reviews and the numbered decisions (D1 to D10) that
this page cites. Operating the daemon is `docs/v1-operations.md`.

Section marks of the form "spec 4a" refer to a private research note that is not part of this
repository. Everything a stranger needs from it is restated here or in the design.

## Processes and layers

One resident Python process, `jarvisd`, started by a Windows scheduled task named `JarvisDaemon`
(at logon and daily at 06:00, interactive, limited rights). Nothing else runs continuously. A
separate small watchdog script under `bin/` is the Phase 0 kill-switch helper.

```
Task Scheduler: JarvisDaemon  (AtLogOn + Daily 06:00, StartWhenAvailable, IgnoreNew)
  action: .venv\Scripts\pythonw.exe -m jarvisd serve --task
      |
      v
daemon.serve -- single instance lock (state/daemon.lock)
  | startup: load config, audit daemon_start (previous exit clean or not), recover jobs,
  |          prune, preflight the claude binary, start the heartbeat
  | APScheduler, memory job store, used as a ticker only:
  |     tick every 120 s and digest cron 06:30 -> reconcile(now) + heartbeat + KILL/PAUSE check
  |     housekeeping daily 04:10 (audit rotation, retention, held expiry); disk check monthly
  |
  +--> reconcile: is today's digest due and absent from the queue? -> enqueue digest-<date>
  +--> worker: claim_next -> digest.run_digest_job
          1 collect    brain | task | git | github | system (| clickup, opt-in)
          2 gate       dispatch.run_gates, per item, fixed order, in code
          3 seal       dispatch.clear_for_claude -> GatedPayload (the only Claude input type)
          4 summarize  claude.ClaudeClient.complete (budget, audit, spawn, settle)
          5 render     render.render_digest (deterministic body, Claude text on top)
          6 write      vault.VaultWriter (atomic, allowlisted places only)
          7 notify     toast, optionally ntfy (counts only)
          8 finish     watermark, run manifest, job to done
          every step writes the hash-chained audit log (ids and counts, never text)
```

Correctness never depends on the scheduler's misfire handling. Every trigger calls one idempotent
`reconcile`, which decides from the queue directories whether today's digest is due. A machine
that slept through 06:30 produces the digest on the first tick after wake, and a duplicate enqueue
is impossible by construction (exclusive file creation). The daemon runs as the human account, not
as a locked-down one, so it is **not sandboxed**; design decision D1 explains why and
`docs/v1-operations.md` lists the compensating controls that live in code.

Layers, top depends on bottom, with no upward imports (a convention; the rules about who may
write files and who may read them are enforced by AST tests in `tests/test_write_locations.py`):

- L0 `common`, `config`, `models`: helpers, typed settings, pydantic models.
- L1 `fsio`, `audit`, `state`, `jobstore`, `vault`: durable IO behind small classes.
- L2 `tier`, `router`, `dispatch`, `claude`: policy, and the one subprocess call site.
- L3 `collectors/*`, `render`, `digest`, `notify`, `local`, `ask`, `consolidate`, `propose`, `hub/*`: job logic.
- L4 `scheduler`, `daemon`, `cli`, `selftest`: wiring.

Folders, all relative to the repository root unless they start with `~`:

- `jarvisd/` the package (tracked). `bin/` and `deploy/` hold PowerShell and the watchdog.
- `queue/{pending,running,done,failed,held}/` one JSON file per job; `held/` holds content-free
  references. Gitignored, and startup refuses a queue path inside the vault.
- `state/` operational state: lock, heartbeat, watermark, budget, breaker, `PAUSE`, `KILL`,
  corrections, per-run manifests, and an empty working directory for the Claude child.
- `logs/jarvisd-audit.jsonl` plus rotated files. Gitignored.
- `~/brain/raw/jarvis/` is where digests and consolidation candidates are written; the vault
  location is configurable.

## Modules

| Path | Purpose |
|---|---|
| `jarvisd/common.py` | time, hashing, canonical JSON, dash stripping, short ids |
| `jarvisd/config.py` | loads `jarvis.toml`, deep-merges the gitignored `jarvis.local.toml`, validates with pydantic; missing `[gates]` is an error (fail closed); never writes config |
| `jarvisd/models.py` | pydantic models: `Item`, `WithheldItem`, `RouterDecision` (exactly seven fields), `GateResult`, `Job`, `RunManifest`, `ClaudeReply` |
| `jarvisd/fsio.py` | atomic write then rename with Windows retry, locked append, file lock |
| `jarvisd/audit.py` | append-only hash-chained JSONL, rotation, `verify`, a redaction guard |
| `jarvisd/state.py` | budget ledger, circuit breaker, watermark, heartbeat, KILL and PAUSE, the daemon lock |
| `jarvisd/jobstore.py` | the JSON-file queue, exclusive-create enqueue, atomic state moves, held references |
| `jarvisd/tier.py` | gate 1: path canonicalization, the hardcoded floor, text scans, and `safe_read_text`, the only read primitive collectors may use |
| `jarvisd/router.py` | the seven-field router contract and its registry; `StubRouter` is the default |
| `jarvisd/dispatch.py` | gates 1 to 3 in fixed order (`decide` is pure), the local-backend seam, `GatedPayload` and `clear_for_claude` |
| `jarvisd/claude.py` | the only module that spawns `claude`; argv, environment, budget, breaker, parsing, runtime isolation checks |
| `jarvisd/local.py` | local-tier adapter: an OpenAI-compatible client, a local router and backend, a four-state tier machine (see `docs/local-tier.md`) |
| `jarvisd/collectors/brain.py` | the notes vault: `RECENT.md` bullets, new session notes, checkpoint counts (counted, never opened) |
| `jarvisd/collectors/task.py` | the active-task state files written by the author's Claude Code setup; replace it for another tracker |
| `jarvisd/collectors/git.py` | read-only git facts for an explicit repository allowlist, through an allowlisted subcommand set |
| `jarvisd/collectors/github.py` | read-only `gh` facts: open and review-requested PRs, latest CI status, stale branch count |
| `jarvisd/collectors/system.py` | "what JARVIS did while you slept", from its own logs; never sent to Claude |
| `jarvisd/collectors/clickup.py` | opt-in ClickUp section through a second isolated `claude -p` call (see `docs/v1-operations.md`) |
| `jarvisd/render.py` | deterministic Markdown for the digest; golden-file tested |
| `jarvisd/digest.py` | the morning digest job handler and the collector registry |
| `jarvisd/ask.py` | `jarvis ask`: one question answered from the latest digest, recent notes and open threads, through the same gates |
| `jarvisd/consolidate.py` | nightly memory candidates (see `docs/consolidation.md`) |
| `jarvisd/propose.py` | task proposals from the latest complete digest run, one gated payload and one capped call, written to `state/proposals/`; creates nothing in a tracker |
| `jarvisd/tracker.py` | tracker adapters behind one Protocol: markdown (default, through the vault writer) and ClickUp (REST, token from the environment); a human click in the Inbox is the only caller |
| `jarvisd/notify.py` | notifier protocol: Windows toast, ntfy push (`docs/notify-ntfy.md`), a null notifier |
| `jarvisd/vault.py` | the only code that writes into the notes vault, three allowed places, enforced by an AST test |
| `jarvisd/scheduler.py` | idempotent `reconcile` and the APScheduler host |
| `jarvisd/daemon.py` | composition root and the resident loop |
| `jarvisd/cli.py` | the `jarvis` command line |
| `jarvisd/selftest.py` | PASS, FAIL and SKIP checks for `jarvis self-test` |
| `jarvisd/hub/` | the read-only web page on loopback (`docs/hub.md`); it imports the layers below it and nothing imports it |

Dependencies: pydantic, APScheduler 3.x (a ticker, behind a small host class), pytest, and for the
hub only fastapi and uvicorn (plus httpx2 for its tests); `docs/hub.md` justifies each pin. Not
used: the Claude Agent SDK (the subprocess is simpler and auditable as an argv list, decision D3)
and any model SDK. `bin/watchdog.py` and `bin/kill-switch.ps1` stay stdlib only.

## Jobs and state

A job is one JSON file in `queue/<state>/<job_id>.json`, written to a temporary file and moved
with `os.replace`. The directory is the truth; the `state` field inside is advisory, and a
mismatch is audited with the directory winning.

- The id is `digest-<local date>`. A forced rerun is `digest-<date>-r2` and writes
  `digest-<date>-r2.md`. A travel across time zones changes the date but cannot double-fire one.
- `origin` is `schedule`, `catchup` (more than 15 minutes late) or `manual`.
- A job carries aggregates filled after gating (router, importance, confidence, sensitive,
  degraded, local tier, cost), a bounded attempt count with backoff, and a history of moves.
- A multi-day gap is coalesced into one digest whose window starts at the watermark, capped at
  72 hours. The watermark advances only after the vault write succeeded.
- A held reference (`queue/held/<item_id>.json`) has an id, a source kind, a reason code and a
  local path reference. It has no title and no text, expires after 14 days, and only
  `jarvis held` shows the path. The design requires that no Claude-bound module reads the folder;
  that is a convention, not something a test enforces.

Other state files, all JSON written atomically: `budget.json` (date, spent, reserved, calls, per
purpose), `breaker.json`, `heartbeat.json` (time, pid, job, version, mode), `PAUSE`, `KILL`,
`corrections.jsonl` (from `jarvis wrong`) and `runs/<job_id>/run.json`, the manifest the hub
reads: stage statuses, counts, cost, paths and hashes, no item text.

## Gate semantics

The gate order is code, not a prompt and not a setting: tier, then importance, then confidence.
`dispatch.decide` is a pure function so the truth table is tested exhaustively. Thresholds and
lists come from `jarvis.toml` `[gates]`, which is human-owned and reloaded per job.

- **Gate 1, tier.** Computed by `tier.item_hit` before the router is ever called. A hit means the
  item is held, or goes to the local tier if one is up and the item is sensitive. The router may
  add sensitivity and can never remove it.
- **Gate 2, importance.** A `high` importance or an escalated category (financial, client-facing,
  irreversible, production) routes to Claude with `confirm_required` set.
- **Gate 3, confidence.** Confidence below 0.72 routes to Claude. The stub router always reports
  0.0, so with no local tier every non-sensitive item exits here, on the real code path.
- **After all gates.** If the local tier is up the item stays local. If it is not installed the
  item goes to Claude and says so. If it is configured but down, the item goes to Claude only when
  it is not sensitive, and the result is marked degraded.

Every decision is one `gate_decision` audit record with codes and ids. Hold kinds are
`sensitive` and `policy`; policy holds cover repositories flagged as work repositories while the
local setting that allows their metadata to reach Claude is off (the default). Policy-held items
still appear in the digest, rendered deterministically; sensitive-held items appear as a count and
opaque ids only.

Gate 1 in practice, all deterministic:

- A hardcoded floor of forbidden vault folders, extended (never reduced) by config globs: the
  floor covers the identity and private-notes folders of the vault and any path component named
  `sensitive`.
- `canonical(path)` rejects NUL and alternate data stream syntax, expands 8.3 short names, resolves
  symlinks and junctions, case-folds and refuses a leftover `..`. Any OS error counts as a hit.
- `path_hit` applies to every path an item carries, including its origin and any path-like value
  in its metadata. `text_hit` applies to titles, text, tags, commit subjects, branch names and
  the final serialized prompt: sensitive tags, `sensitive: true`, hashtags, and the configured
  terms, case-insensitive and accent-folded. A hit code never echoes the matched term.
- `safe_read_text` is the only read primitive collectors may use. It refuses paths outside the
  declared source roots without opening them, returns a `WithheldItem` on a path hit without
  opening the file, and scans the text of what it read. A collector that splits a file into
  independent bullets may scan each bullet itself, so one hit holds one line, not the file.

Sealing. `dispatch.clear_for_claude` is the only constructor of `GatedPayload`. It drops every item
not routed to Claude, re-runs `tier.item_hit` on what is left, applies the payload size cap by
priority, renders each item as id, source, title, text, time and a policy marker (no paths),
neutralizes a closing data tag, hashes the result and runs `tier.assert_clean` on the final text.
A hit aborts the call, audits `tier_violation`, opens the breaker and renders the digest without
Claude, with a loud line. `ClaudeClient.complete` raises `TypeError` for anything that is not a
`GatedPayload`.

## Calling Claude

`claude.py` is the only place a `claude` process is started. One call shape, a list argv (no
shell), the prompt on stdin, an empty working directory:

```
claude -p --output-format json --model <model>
  --setting-sources ""            keep the user's CLAUDE.md, hooks and skills out
  --disable-slash-commands        no skills
  --strict-mcp-config             with no config given, zero MCP servers load
  --tools ""                      no built-in tools: single turn, no files, no shell
  --no-session-persistence        no transcript for other tooling to pick up
  --permission-prompts none       anything that would prompt is denied
  --max-budget-usd <cap>          per-call spend cap
  --system-prompt <constant>      the data block is declared untrusted
```

Why each flag: they were measured on the author's machine, where an isolated trivial call costs
about 437 input tokens and a fraction of a cent, while an unisolated call loads tens of thousands
of tokens of local configuration. The `--setting-sources ""` plus OAuth behaviour is observed, not
documented, so a runtime token tripwire and the CLI version in every call record watch for drift.

Environment. Built from scratch from a short list of Windows variables plus a marker. Every
`ANTHROPIC_*` and `CLAUDE_*` variable is dropped, so a stray API key cannot switch billing.

Cost control, all before the process is spawned, in code:

1. The per-call `--max-budget-usd`.
2. A ledger reservation: refused when spent plus reserved plus the cap would exceed
   `daily_budget_usd`, or when the daily call count is used up. The reservation is persisted
   before the `claude_intent` audit record, so a crash mid-call still counts the full cap.
3. At most two attempts per call and three per job, with backoff.
4. A circuit breaker: three consecutive failures or one rate-limit answer opens it for an hour; a
   privacy event (payload block, isolation breach, `jarvis wrong --leak`) opens it until a human
   runs `jarvis breaker reset`. While it is open the digest ships without Claude.
5. A timeout, and a `state/KILL` file polled every second while the child runs; it terminates the
   child and the daemon exits.
6. A network-readiness wait after wake, so a call is not wasted on a dead connection.

Runtime isolation checks (defence in depth; the gate is the control). A token tripwire discards a
reply whose input token count shows configuration leaked in. A before-and-after snapshot of the
vault's session folders treats any new file as a breach, because it means another tool's hook ran.

Failure handling. The digest always ships. A timeout or transient error retries once; a rate limit
or sign-in failure opens the breaker and the note is deterministic; a malformed reply is not
retried (paid) and its raw output is kept under `state/runs/` for debugging. Every item Claude
did not summarize is listed under "Held back and not summarized" with a reason, never silently.
A daemon started by hand without `--task` runs with Claude disabled, so there is no unkillable
spender.

`jarvis ask`, `jarvis consolidate` and `jarvis propose` reuse this client and its ledger without changing the isolation:
they supply a different constant system prompt and parser. The optional ClickUp section is the one
exception. It is a separate profile that passes neither `--tools ""` nor `--strict-mcp-config`,
because the connector is not in a config file and must stay visible to the call. Its isolation is
an allowlist of two read tools, a denylist that is a snapshot of the connector's tools, and
`--permission-prompts none`; a tool the connector adds later is refused only by the permission mode.
It is off by default. The profile and its injection surface are in `docs/v1-operations.md`.

## Where to go next

- Rationale, decisions and the open questions: `docs/v1-design.md` and `docs/v1-plan.md`.
- Running it: `docs/v1-operations.md`.
- What the audit log holds and how to verify it: `docs/v1-operations.md`, "Reading the audit".
- The Phase 0 isolation work and the sandbox policy: `docs/phase0.md`, `docs/sandbox-policy.md`.
