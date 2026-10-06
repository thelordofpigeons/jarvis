# JARVIS v1 operations (option 2: morning digest)

How to run, stop, pause and inspect `jarvisd`. Design references are to
`docs/v1-design.md` by section number. The daemon is observe-only: it reads, builds one
digest note under `brain/raw/jarvis/` and shows a toast. It acts on nothing.

Commands below use `jarvis`, which is `jarvis.cmd` in the repo root (or
`.venv\Scripts\python.exe -m jarvisd`). Exit codes: 0 ok, 1 failure, 2 usage, 3 refused by a
safety control (kill, pause, breaker, budget) or killed (section 11).

On this machine PowerShell blocks unsigned scripts by default, so run the deploy scripts as
`powershell -NoProfile -ExecutionPolicy Bypass -File deploy\<script>.ps1`.

## What runs and as whom

`jarvisd` runs as `<owner>` in an Interactive scheduled task named `JarvisDaemon`
(section 1, D1). It is not sandboxed and not isolated by an OS boundary. The safety comes
from code: a fail-closed tier gate that runs before any file is read, a Claude call with an
isolated flag set and no tools, one vault writer, and a budget ledger with a circuit breaker.
The audit log is tamper-evident, not tamper-proof (section 13).

## Start

First time on a machine:

1. `powershell -NoProfile -ExecutionPolicy Bypass -File deploy\setup-venv.ps1` (creates
   `.venv` and `requirements.lock`; safe to repeat).
2. Copy `jarvis.local.toml.example` to `jarvis.local.toml` and fill in your own repo list,
   `[gates].sensitive_terms` and any extra sensitive globs. This file is gitignored; private
   names live only there (section 1, D10).
3. `jarvis self-test` and read every line.
4. Register the task: `powershell -NoProfile -ExecutionPolicy Bypass -File deploy\register-jarvisd-task.ps1 -WhatIf`
   to see the plan, then the same command without `-WhatIf`. `jarvis install-task` prints
   the same block and `jarvis install-task --apply` runs it.
5. Start it now instead of waiting for the next logon: `schtasks /run /tn JarvisDaemon`
   (from Git Bash set `MSYS_NO_PATHCONV=1` first so `/run` is not rewritten as a path).

Normal days: the task starts at logon and at 06:00, takes `state/daemon.lock`, and a second
instance exits 0 straight away. The digest is due at 06:30 local. If the machine slept
through it, the digest appears at wake or logon and says `late: true` when generated after
12:00 (section 10).

Dev mode: `jarvis serve` without `--task` runs with Claude disabled and without the
task-disabled check. It is not covered by the kill switch, so never leave it running.

## Stop

- Normal stop: `Stop-ScheduledTask -TaskName JarvisDaemon`. The next logon or 06:00 trigger
  starts it again.
- Stop and keep it stopped: `Disable-ScheduledTask -TaskName JarvisDaemon`. The daemon also
  notices a Disabled task and exits 0.
- Hard stop: create the file `state/KILL` (any content). The daemon exits 3 within a
  second during a Claude call and at the next tick otherwise, and refuses to restart until
  the file is removed. `jarvis status` shows it.
- Machine-level kill: `schtasks /run /tn JarvisKillSwitch` runs `bin/kill-switch.ps1`. Daemon
  scope writes `state/KILL`, stops and disables `JarvisDaemon`, and kills the daemon's process
  tree (the heartbeat pid, its launcher and every child, so the `claude` call too) plus any
  orphaned isolated `claude`. Every step is one action in `logs/killswitch.jsonl`
  (`docs/killswitch-v1-patch.md`, applied; hostile simulation results in the run log below).
  The egress rule is keyed to the `jarvis` account and reports `not-applicable` for an
  `<owner>` daemon. After a real trip, recover with `Remove-Item state\KILL` and
  `Enable-ScheduledTask -TaskName JarvisDaemon`; the daemon refuses to start while the file exists.

Remove the task entirely: `deploy\register-jarvisd-task.ps1 -Unregister`.

## Pause and resume

- `jarvis pause --for 4h --reason "travelling"` writes `state/PAUSE`. Enqueue and claim stop;
  the heartbeat continues, so `jarvis status` still shows a live daemon.
- `jarvis resume` removes it.
- A manual `jarvis run-digest` ignores pause (you asked for it) but still honours `state/KILL`.

## First-run checklist

Do these in order on the first real day. Nothing here is skippable because the privacy model
is heuristic for untagged personal content in session notes (section 16).

1. `jarvis self-test` is all `[PASS]` or an explained `[SKIP]`. `[FAIL]` blocks.
2. `jarvis audit verify` says the chain is ok (it says no records yet on a new machine).
3. Fill `jarvis.local.toml`: repos, `sensitive_terms`, `sensitive_path_globs`,
   `[hygiene].deny_substrings` (the hygiene test reads it, so the names never have to be
   written into a tracked file). Decide the open question on `work_metadata_to_claude`
   (section 17, question 1); it stays `false` until you confirm it.
4. `jarvis run-digest --dry-run`. Read the printed payload and the held list. This is the
   exact text that would leave the machine. If anything private is in it, add a term or glob
   to `jarvis.local.toml` and repeat. Do not continue until the payload is clean.
5. `jarvis run-digest` (deterministic, no Claude). Open the note it names under
   `brain/raw/jarvis/`.
6. `jarvis run-digest --claude --force` once, with the budget cap in `jarvis.toml` as the
   limit. This is the first paid call. Judge whether the note is useful.
7. `jarvis self-test --live` for the paid isolation smoke (the paid check is not part of
   this build; the command prints a SKIP line and spends nothing).
8. Register the task, confirm with `jarvis status` that the heartbeat is fresh, and let the
   next 06:30 produce the note unattended.
9. Read the next morning's note and `jarvis wrong <id>` anything that was misjudged.

## Reading the audit

`logs/jarvisd-audit.jsonl` has one JSON object per line: `ts`, `event`, `seq`, `prev`, `h`,
`run_id`, `job_id`, `pid`, `ver`, then fields. It holds ids, hashes, counts and costs, never
item text (section 13).

- `jarvis audit tail -n 100` shows the last 100 records.
- `jarvis audit verify` checks the hash chain on the live file and prints the first broken
  `seq`; `jarvis audit verify --all` also walks rotated files. Exit 1 on a break.
- `jarvis audit cost --days 7` sums settled Claude spend per local day.
- Each digest note carries `audit_seq` and `audit_head` in its front matter. Because `brain/`
  is synced, comparing them with the local log detects a later rewrite of the log. Tail
  truncation is only detectable this way.
- Useful events when something looks wrong: `daemon_start` (has `previous_exit`: clean,
  unclean or first), `claude_intent` without a `claude_call` (a call died mid-flight; the
  reservation is already counted), `gate_decision` (why an item was held), `items_held`,
  `tier_violation`, `isolation_anomaly`, `isolation_breach`, `breaker`, `budget_refused`,
  `ask_intent` and `ask_call` (a `jarvis ask`), `clickup_check` and `injection_signal` (the
  ClickUp section: a tool outside the allowlist or a permission denial in the reply).
- Rotation: monthly or at 20 MB to `logs/jarvisd-audit.<UTC>.jsonl`; files older than 180
  days are deleted at housekeeping (section 10).

## Asking a question (`jarvis ask`)

`jarvis ask "what is open on the exporter"` answers one question from what JARVIS already
holds, with one Claude call. Quotes are optional. Nothing is scheduled; it is a manual
command and it never writes to the vault.

Sources, nothing else: the latest digest note, the bullets of `brain/RECENT.md`, and the
`## Next session entry point` and `## Open threads` lines of session notes from the last
seven days. Everything goes through the same gates as the digest (design section 6):

- Gate 1 first: path, tag, `sensitive: true` and the per-line term scan. A line that hits is
  withheld on its own; its neighbours still flow. `telos/`, `notes/` and anything under a
  forbidden root are never opened.
- A digest note also prints items that Claude never saw (policy-held work metadata) and the
  ids of sensitive ones, so its lines are not trusted because JARVIS wrote them. A digest line
  is offered only when it carries an item id and the audit's latest `gate_decision` for every
  id on that line says `claude`. Lines without an id, the front matter and the held list stay
  out. The headline is offered only when the front matter says `claude: ok`.
- Then the router, the sealing step (`clear_for_claude`) and the client's final scan of the
  whole prompt, question included. A question that names a held term is refused and not
  echoed.
- Items the router sends to the local tier (when it is up) are not offered to Claude, so with
  the local tier on the answer may know less than the digest does.

The call is the digest call with a different constant system prompt (`ASK_SYSTEM_PROMPT` in
`jarvisd/ask.py`): same isolation argv, no tools, the data block declared untrusted, a JSON
reply of `answer` and `ids`. The ids are checked against the ids that were sent and an
invented one is dropped and counted. It uses the ledger like any other call (cap
`[claude].max_budget_usd`, one of `daily_calls`), the breaker and the kill file.

- `jarvis ask --dry-run "..."` prints the header, the exact payload, the size and hash, and
  the held list (id, kind, reason; never content). It spawns nothing and reserves nothing.
- Exit 0 answered, 1 nothing to answer from or the call failed (`bad_json`, `timeout`, ...),
  2 usage, 3 refused by a safety control (open breaker, used-up budget, kill file, a
  question that hits the tier gate, a sealing block).
- Audit: `ask_intent` (question hash and length, counts, payload hash) before the call and
  `ask_call` (ok, kind, ids used, hallucinated ids, cost) after it, next to the usual
  `claude_intent` and `claude_call` with `purpose: ask`. No text is ever audited.

## ClickUp section (behind a flag)

Off by default (`[digest].clickup_enabled = false`). When on, the digest gains a `## ClickUp:
open tasks` section made by a second, separate `claude -p` call (profile `clickup_read`)
through the claude.ai ClickUp connector. If it cannot be produced the section says
`ClickUp: unavailable (<reason>)` and the digest still ships.

Turning it on, in this order, on the machine that runs the daemon:

1. `jarvis clickup check` (free): binary, the flags `claude --help` must list, the argv
   invariants, the empty working directory.
2. `jarvis clickup check --live` (one real call, up to `[digest].clickup_max_budget_usd`,
   default 0.10 USD): it must return a JSON array of tasks, with no permission denial and
   only allowed tool uses. It records the result and the hash of the flag set in
   `state/clickup-check.json`. If the model reached for ToolSearch the check says so; set
   `[digest].clickup_allow_tool_search = true` in `jarvis.local.toml` and run it again.
3. Set `clickup_enabled = true` (and optionally `clickup_user_id`, digits only, your ClickUp
   user id; empty means the account the connector is signed in with) in `jarvis.local.toml`,
   then restart the daemon (`Stop-ScheduledTask`, then `schtasks /run /tn JarvisDaemon`):
   the collector list is built at start. Until step 2 has passed for the current flag set the
   collector answers `check_not_run`, `check_failed` or `check_stale` and spends nothing.

The call:

- Prompt: a constant (`CLICKUP_TEMPLATE`) with three validated tokens (assignee, a due date
  48 hours ahead, the window start date). No vault text, no item text, no file.
- Flags: `--output-format stream-json --verbose --model haiku --setting-sources ""
  --disable-slash-commands --no-session-persistence --permission-prompts none
  --max-budget-usd 0.1 --allowedTools <two read tools> --disallowedTools <every other
  connector tool and every built-in tool> --system-prompt <constant>`. Not used:
  `--strict-mcp-config` (the connector is not in a config file) and `--tools ""` (whether it
  hides connector tools is for the live check to say, so built-ins are denied by name).
- Working directory `state/claude-cwd`, empty. Environment: the same allowlist as the digest
  call, no API key, no ClickUp credential. The old `clickup-config.json` is not read.
- Allowed: `clickup_filter_tasks`, `clickup_get_task`. The denylist names the other 59
  connector tools as of 2026-10-05 (writes and reads of chat, documents and time entries
  alike) and `Bash`, `PowerShell`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `WebFetch`,
  `WebSearch`, `Task`, `ToolSearch` and the rest of the built-ins.
- Cost control: the ledger reserves the 0.10 USD cap before the spawn, the daily budget and
  call count apply, the breaker and `state/KILL` apply, the wait is `[digest].clickup_timeout_s`
  (75 s) with no network wait. The token tripwire is off for this profile because the
  connector schemas alone are about 27k tokens.

The injection surface, stated plainly. This is the widest in the design (D7):

- Untrusted input: task names, descriptions, comments and custom fields of every task the
  account can see. Anyone who can create or edit a task you can see can put text in front of
  the model. The model reads it as a tool result.
- What hostile text can try: call other tools of the connector (write or delete tasks, post
  chat messages, read documents), call tools of any other connector or MCP server of the
  account (`--strict-mcp-config` is not used, so they are visible to the model and only
  refused), call file, shell or web tools, or bend the reply (invent, hide or reorder tasks,
  put long text or links in a name).
- Controls: the allowlist and denylist above; `--permission-prompts none`, so anything not
  allowed is refused and listed in `permission_denials`; an empty working directory; a
  constant prompt with token-only parameters; no secret in the environment; the budget cap.
  After the call: any tool use outside the allowlist or any permission denial rejects the
  whole reply unread (`tool_violation`) and writes an `injection_signal` audit record with
  the tool names and the denial count. A tool violation does not open the breaker (it is not
  evidence about the service) and the money is still counted. A sign-in failure (`auth`)
  from this call counts as one transient failure toward the breaker instead of the human-reset
  trip, because it may be the ClickUp connector's own login lapsing and must not also stop
  the digest's summarize call.
- The reply is parsed as untrusted: strict models, ids and dates validated, unknown fields
  dropped, names cut to one line, at most 30 tasks (more is `partial: over_30`), a task that
  fails validation is dropped and counted. The task link is rebuilt from the validated id; a
  URL from the model is ignored. Free text is kept out of `meta` (the tier gate reads
  path-like `meta` strings as paths). The items are then marked `work`, so they go through
  gate 1 like any other item and, unless `work_metadata_to_claude` is on, are rendered in the
  digest and never sent to the summarizing call.

What this does not protect, and what is not verified:

- The profile cannot hide the other connectors from the model, only refuse their tools. If
  the CLI treated an allow or deny entry differently from this document, a refusal could turn
  into an execution. `jarvis clickup check --live` runs one benign request; it cannot prove
  that a hostile one is refused. Read the `injection_signal` records when they appear.
- ClickUp content, including tasks you would call private, reaches the Anthropic API during
  this call. That is the same boundary as using the connector in Claude Code by hand, but it
  is a new automatic flow for the daemon. It is your decision, which is why it is opt-in.
- The assignee, status and date filter is the model's reading of a prompt, not a query this
  code ran. A task can be missing or wrongly included, and the digest prints the count.
- Not run against the real connector by whoever wrote this: the comma syntax of
  `--allowedTools`, whether the connector tools are reachable without ToolSearch in headless
  mode, the stream-json event shapes the parser expects (`tool_use` blocks inside `assistant`
  events, a final `result` event with `permission_denials`), and whether 0.10 USD is enough
  with the connector schemas loaded (the plan estimated 0.19 USD). A cap that is too low shows
  as `budget` in the collector and as `error_max_budget_usd` in the call record; raise
  `[digest].clickup_max_budget_usd`. All of it is tested against `tests/fakes/fake_claude.py`.

Failure codes in the section line: `check_not_run`, `check_failed`, `check_stale` (run the
live check), `breaker`, `budget`, `killed`, `network`, `timeout`, `auth`, `rate_limit`,
`transient`, `tool_violation`, `bad_json`, `bad_schema`, `empty_reply`, `isolation_breach`.

## Where state lives

All gitignored: `queue/{pending,running,done,failed,held}` (jobs and held references),
`state/` (budget, breaker, watermark, heartbeat, `daemon.lock`, `KILL`, `PAUSE`,
`corrections.jsonl`, `runs/<job_id>/run.json`) and `logs/`. Delete nothing by hand while the
daemon runs. `jarvis held [<id>]` is the only place a held item's source path is shown.

## Incident runbook

For "the daemon did something I cannot explain" or "something private reached a note"
(section 16):

1. Stop it: run `schtasks /run /tn JarvisKillSwitch`, or create `state/KILL`.
2. Read `jarvis audit tail -n 100` and run `jarvis audit verify --all`.
3. If a leak is suspected: `jarvis wrong <id> --leak`. This opens the breaker (it needs a
   human reset) and prints the runbook. Read the payload archive if `archive_payloads` was
   on, and `state/runs/<job>/`.
4. Nothing needs rotating in v1 because no outward token exists. The Claude login is the
   owner's own; `claude /login` again if you want to be sure.
5. Add the missed term or glob to `jarvis.local.toml`, write a session note by hand, then
   `jarvis breaker reset --reason "..."` and remove `state/KILL` once you understand it.
   The daemon re-reads `jarvis.toml` and `jarvis.local.toml` at the start of every tick (two
   minutes), so no restart is needed. A misspelled key or table in the local file is an
   error, not a silent default: the daemon audits `config_invalid`, runs no job until the
   file is fixed, and keeps gating with the last good rules in the meantime.

Common non-incidents: `claude_status: budget` (daily cap reached, deterministic note only),
`claude_status: auth` (run `claude /login`, then `jarvis run-digest --claude --force`),
`breaker open` after three failures or one 429 (`jarvis breaker status` shows the reason and
the time it clears; a leak or isolation breach needs `jarvis breaker reset`).

A known false positive: a second Claude Code session that writes a checkpoint or session note
during a digest call looks like an `isolation_breach` and opens the breaker. Syncthing's own
temp files and notes it delivers during the call (they keep the sender's older modification
time) are ignored; a delivery stamped by a clock that runs ahead of this machine's still counts.

If the vault is busy when the note is written, the job retries (the Claude summary is kept, so
the retry costs nothing). After the last attempt the job fails with a toast, and the rendered
digest is under `state/runs/<job_id>/digest-unwritten.md`.

`jarvis run-digest` before the scheduled time (06:30) without `--claude` writes
`digest-<date>-r2.md` and leaves the scheduled job free to run with Claude. After an auth
failure the day's job is already done, so the way back is `jarvis run-digest --claude --force`.

## The local tier and the other optional parts

The local tier is an adapter (`jarvisd/local.py`), off by default and tested against a fake
server only. While `[local].enabled` is false it reports `not_installed`, no socket is opened
and sensitive items are held, not routed local. How to install a model server, switch the adapter
on and what is and is not proven is in `docs/local-tier.md`. The gate behaviour with the tier
present or absent is in `docs/architecture.md` (gate semantics) and design section 6.

Each optional part has its own page, with its status stated at the top:

- Phone push through a self-hosted ntfy: `docs/notify-ntfy.md`.
- The work hub on loopback, read-only except the Inbox: `docs/hub.md`.
- Nightly memory candidates (`jarvis consolidate`): `docs/consolidation.md`.
- The ClickUp section and `jarvis ask`: above on this page.

Everything the hub and later phases read from v1 is three artifacts: `queue/*/` job files,
`state/runs/<job_id>/run.json` and `logs/jarvisd-audit*.jsonl`. A new collector or digest section
is one class or one renderer. The Claude bridge replaces the subprocess runner behind
`ClaudeClient`; `GatedPayload` and the gates do not change.

## Kill switch for the v1 daemon

`bin/kill-switch.ps1` Daemon scope finds the v1 daemon by what it is, not by who owns it: the
pid in `state/heartbeat.json` (refused unless the process behind it is a `jarvisd`), the venv
launcher above it, every descendant (so the `claude` call), any process carrying the command
line marker `jarvisd serve --task`, and any orphaned `claude` with the isolated argv. It then
writes `state/KILL`, stops and disables `JarvisDaemon`, and kills what it found. The audit line
in `logs/killswitch.jsonl` has one action per step (`daemon-lock`, `daemon-targets`,
`create-kill-file`, `stop-task`, `kill-daemon-tree`, `kill-processes`, `revoke-egress`). Details
and the list of what differs from the first proposal: `docs/killswitch-v1-patch.md`.

Verification run, 2026-10-05, non-elevated, on the machine that runs the daemon:

- `powershell -File tests\hostile-sim.ps1`: **27 of 27 checks passed**, in about 25 s, in dry-run
  and real mode, against stand-ins only. Four scenarios: the account branch (3 looping
  processes, selected by owner and marker); a daemon under a stand-in scheduled task (venv
  `pythonw` launcher, python child, claude-like grandchild, heartbeat file, a daemon that
  ignores `state/KILL`); the same daemon without a task (found through the heartbeat pid and
  its tree); a stale heartbeat naming an unrelated process plus an orphaned claude-like process.
  Dry runs killed nothing, created no `state/KILL` and left the task enabled. Real runs left
  zero survivors, disabled the stand-in task, wrote `state/KILL` in the stand-in state folder and
  appended one audit line each. An unmarked python and an unmarked powershell survived every
  scenario. The live `JarvisDaemon` task, its heartbeat pid, `state/KILL` and
  `logs/killswitch.jsonl` were compared before and after: unchanged.
- A read-only dry run of the real script against the live daemon (audit redirected to a temp
  file) reported `daemon-lock held`, the targets `launcher` and `daemon` (the venv launcher and
  its python child), `create-kill-file` and `stop-task` as would-do lines, and no other python
  process. Nothing was stopped.
- `python bin\watchdog.py --self-test`: 15 of 15. `jarvis self-test`: 13 of 13, including
  `killswitch-alignment`, which now reads the script and compares its task name and marker with
  the daemon's.

Not verified: the elevated path (the firewall rule, which only applies to a `jarvis` target), the
registered `JarvisKillSwitch` task running the new script end to end, and a real trip against the
live daemon (it stops the digest until you recover with `Remove-Item state\KILL` and
`Enable-ScheduledTask -TaskName JarvisDaemon`). Do that once, at the machine. The simulation
cannot prove Task Scheduler's own behaviour on the elevated task or on a locked-down machine.

## Known gaps (design section 16)

Stated rather than hidden:

- Section 9a isolation is not met (D1). The compensations are in code, not an OS boundary.
- The SID-keyed egress rule does not cover an `<owner>` daemon (it would cut the owner's own
  network if widened). The kill switch's process-kill step now does (`docs/killswitch-v1-patch.md`,
  applied and simulated), and `state/KILL`, task stop and the task-disabled self-exit remain as
  independent paths. Not yet exercised: a real trip of the elevated `JarvisKillSwitch` task
  against the live daemon.
- The audit is tamper-evident, not tamper-proof. A process under the same account can
  rewrite the file and the chain together; the digest witness (`audit_seq`, `audit_head`)
  is the out-of-band check.
- The digest needs you logged on (Interactive task). A sleep through 06:30 gives a late
  digest.
- The privacy model is heuristic for untagged personal content in session notes. Populate
  `[gates].sensitive_terms`, review `run-digest --dry-run` before the first real run, and use
  `jarvis wrong <id> --leak` on any miss.
- The `--setting-sources ""` plus OAuth behaviour of the Claude CLI is observed, not
  documented. Mitigations: the live smoke, the CLI version in every call record, the token
  tripwire and the deterministic fallback.
- Log rotation exists for v1 files only; Phase 0 logs have none.
- The toast uses a JARVIS AppUserModelId copy of the hooks script (`deploy/notify-jarvis.ps1`).
  If the global hooks script changes, the copy does not follow.
- Not built: a job that feeds held items to a local model, Slack, voice, any outward action,
  running under the `jarvis` account, `srt` wrapping of daemon actions, and any action taken
  from the hub page. Built but off by default and not proven against the real service: phone
  push, the local tier, consolidation and the ClickUp section (see the status table in the
  README).

The author's log of the first live run is in `docs/first-run-log.md`.
