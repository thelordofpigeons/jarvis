# Changelog

All notable changes to this project. Dates are the day the work was verified on the author's
machine. The format follows Keep a Changelog; versions are not published to PyPI.

## Unreleased

### Digest writer: item state and the weekly review (`docs/hub-rework-contract.md` sections 6 and 9)

Phases 3 and 4 of the rework, writer side. Verified with the tests and `jarvis run-digest --dry-run`
(nothing written; the dry note is rendered in memory with the same rules). No real digest was run.

- Item sidecar: after the note is in the vault, `jarvisd/digest.py` writes `state/runs/<job>/items.json`,
  one record per rendered item line (`key`, `id`, `section`, `group`, `date`, `text`, `rank`, `since`),
  read back from the rendered sections by `render.sidecar_items`: a sensitive-held id has no line,
  so it has no record. Nothing is written on a dry run or after a refused vault write.
- Item history: `state/item-history.json` (`state.ItemHistory`, `update_history`), the writer's memory
  per key: first and last seen, times shown, one section entry per run, status open, done, snoozed
  or dropped. Read-modify-write under the state lock; the digest is its only writer.
- Aging, applied while the note is built (`render.Aging`, `render.aging_from`): a done key is out of
  every section for ever; a snoozed key is out of Start here, Still open and the fallback until its
  date, then back with `since: returned` (a changed text is a new key, back at once); a key shown
  twice in Start here (`render.pick_start_here`, budget 2) moves to Still open with its age; a
  decision is filed done at first print, so a rerun cannot repeat it; an open key idle for more than
  seven days is dropped and listed in the weekly review, and reopens with its old `first_seen` when
  collected again. The attention decisions under `state/attention/` are read before rendering.
- Front matter gains `n_since_new`, `n_since_resolved`, `n_since_dropped`, `n_since_returned`; the
  goldens were regenerated once, deliberately, for those four lines.
- Weekly review: `raw/jarvis/weekly-YYYY-Www.md` (`jarvisd/weekly.py`, `render.render_weekly`), written
  by the first digest of a new ISO week when the file for the week just ended is absent and the week
  has something to review, and by the new `jarvis weekly [--week YYYY-Www] [--dry-run]`, through the
  same vault writer and marker rule as the digest. Six sections with one regex per line: Runs,
  Decided this week, Dropped threads, Snoozed and done, Flagged wrong (ids only), Cost by day;
  `- None.` when empty, newest first, cap 30. Audit events `items_recorded` and `weekly_written`
  (counts, week, path and hash; never text).
- Item keys: brain items keep `meta["key"]`; a repo, task or GitHub item takes its title, because its
  text changes with every commit subject and a key that moved every morning could never be snoozed.

### Hub: Done, Snooze, seen marks and This week (`docs/hub-rework-contract.md` sections 7 to 9, hub side)

Phases 3 and 4 of the rework on the hub side, verified offline and in a browser at 1440 and 375 px on
2026-10-09. No real digest line was decided; the routes met the test tree only.

- Every Needs you and Still open line carries `data-key` (the sidecar's key, or the normalised text for a note
  written before the sidecar) and `data-id`, the text in `.t` and, when the id is in the latest run's
  `state/runs/<job>/items.json`, a Done button and a Snooze disclosure (Tomorrow, 3 days, Monday, a date) in
  `.act`: one row at 1440, the controls wrap under the text at 375, 44 px tap targets under 40rem.
- Two new POST routes, `/today/<id>/done` and `/today/<id>/snooze`, one handler `decide_item` in
  `jarvisd/hub/app.py`, the write path in the new `jarvisd/attention.py` (beside `jarvisd/inbox.py`): the Inbox
  guards in the Inbox order, the id checked against the sidecar (404) and the key taken from there, `until`
  validated (422), one lock (`state/attention/decide.lock`, busy is 409), one file per key under
  `state/attention/` through `atomic_write_text`, a decision in force is 409 and an expired snooze is replaced,
  audit `attention_decided` and `attention_decide_failed` (502) with ids only, 303 to Today with a banner.
  `data.today()` hides a line with a decision in force at once and says "N lines decided, applied at the next
  digest". `cmd_attend` is ready for the `jarvis attend` wiring on the CLI side.
- Changed since yesterday gains the four thread lines of contract 6.2 (`n_since_*` from the front matter, texts
  from `state/item-history.json` and the sidecar, five at most).
- Seen marks: a second block in `/static/prefs.js` keeps `hub:seen` (digest id, keys, previous keys) in
  `localStorage`, rolls it only when `<section id="today" data-digest>` changes, and adds class `seen` to a line
  carried over from the last digest seen (`.seen .t` at 60 percent). A reload or the 30 s refresh stores nothing;
  no network, no cookie. The rule runs under node in `tests/test_hub_face.py`.
- Activity gains `<details id="week">` "This week": the newest `weekly-YYYY-Www.md` parsed by
  `digestparse.parse_weekly` (the six sections of contract section 9, regex per line, raw lines kept), then
  "Flagged wrong" from the `correction` audit events of the last 7 days in the label map's words, shown even
  without a weekly note.
- `docs/hub.md`: the security model names the second write path and the five POST routes; "Done and Snooze on
  Today" and "Item keys and seen marks" are new sections; the views table and the status rows are updated.
- Tests: `tests/test_attention.py` (new, mirrors `tests/test_inbox.py`), Today and Activity tests in
  `tests/test_hub.py` with a sidecar and a history in the fixture, `parse_weekly` in `tests/test_hub_digestparse.py`
  on a synthetic note and on real `render_weekly` output, the seen-marks harness in `tests/test_hub_face.py`.
  The GET never-writes test, the 405 test, the CSP, the import and method bans, `NAV`, `check.VIEWS` and the CSS
  bounds are unchanged.

### Hub: four views (`docs/hub-rework-contract.md` sections 2 to 5)

The hub half of the rework, verified offline and in a browser at 1440 and 375 px on 2026-10-09. Nothing was
sent, confirmed or written outside the tests.

- Four views: Today, Inbox, Projects, Activity, plus `/face`. `/runs`, `/ledger`, `/held`, `/audit`, `/status`,
  `/repos` and `/reminders` answer 301 to the view that absorbed them (`REDIRECTS` in `jarvisd/hub/views.py`).
- Today is rebuilt from the parsed note (`jarvisd/hub/digestparse.py`, grammar 2 with a grammar 1 fallback,
  raw lines kept when a pattern misses): Attention, Needs you, Waiting for you (three oldest proposals with an
  inline Confirm, `next` accepted only as `/` or `/inbox`), Changed since yesterday (the delta against the
  previous note), then Everything else, Full digest and Item ids behind `<details>`, and "That's all." Ids
  leave the visible text and live in `data-id`.
- A one-line status strip opens every page (health word, last digest, next digest, waiting, failed) with the
  avatar at its right end at every width; the health word is the one live region and the daemon pill is gone.
  Under 40rem the strip shows a shorter line. `/api/status` adds `last_digest.at`; `face.js` repaints the
  strip on each poll.
- Review fixes (phase 2 verification): 44px tap targets under 40rem (tabs, avatar link, buttons, inputs,
  the `tap` links); local type stated and argued in `hub.css` (Bahnschrift, then Segoe UI Variable, with
  Cascadia Mono for code); spacing and radius tokens; hover and focus states; Inbox actions as one row of
  three controls with the Edit and Reject forms opening below; the Inbox and Projects explanations behind a
  "How this works" disclosure; Activity tiles five per row from 64rem with "Tomorrow 06:30"; Activity gets
  h2s ("Last 7 days", "Records"); Projects drops the Days idle column, prints zeros as empty cells, colours
  housekeeping risks amber and real breaks red, anchors rows for the Today Attention link; the Daemon status
  disclosure is a definition list with the CLI block nested; the grammar 1 fallback de-duplicates Still open
  on `norm_key`, applies the writer's exclusions and decodes the System counters.
- Writer: the fallback Start here template is verb-first, "before" and "by" are no longer deadline words,
  Claude whys must read as an order (`render.imperative`), reference lines ("Related: ...") are noise,
  decisions are cut at a dash before `clean()`, a thread that quotes the active task id is not repeated
  under Still open, and the prompt no longer asks for per-item summaries.
- Inbox cards are slim (title, why, pills, three buttons; identifiers and evidence behind a disclosure),
  `?sort=due` sorts by due date, unresolvable evidence ids collapse to one line.
- Projects absorbs Repos: Active first, "Quiet: N repos" with a disclosure, the old table behind
  `raw-repos`, a card list under 40rem. New key `[hub].always_dirty` (default empty).
- Activity absorbs Runs, Ledger, Held, Audit and Status behind a 7-day tile strip and five disclosures.
  Jargon goes through one label map (`docs/hub.md`).
- `/static/prefs.js` remembers open disclosures per page in `localStorage` and restores them after the
  refresh (`hub.js` now dispatches `hub:refreshed`).
- Performance: `/api/status` reads the three newest files of `queue/done` and `queue/failed` and nothing else
  once warm (held, proposals, manifests, the newest queue files and the digest notes are cached by directory
  signature); the audit witness is an index lookup; one `HubData` method per route.

### Digest writer: grammar 2

Digest grammar 2 (`docs/hub-rework-contract.md` section 1), the writer half of the hub rework. The
audit of 2026-10-09 found the note unreadable: every open thread printed twice (once from
RECENT.md, once from its session note, with two ids), ten decisions a day with their rationale,
hashes and a second language glued to each line, raw counters and an undecoded Windows result
code under System. Verified with `jarvis run-digest --dry-run`; nothing was written.

### Changed
- `jarvisd/render.py`: sections are now Start here, Attention, Active task, Still open, Decided
  yesterday, Repos, System, Held back and not summarized, Source status, Flag a mistake. Every item
  line ends in one id tail ` [xxxxxxxx]` and nothing follows it; Claude one-liners are no longer
  glued to lines. `GRAMMAR`, `ID_TAIL` and `HEADINGS` are exported for the hub. New front matter
  keys: `grammar` and twelve `n_*` counters.
- Start here lines are verb first with a deadline or a consequence; the Claude prompt
  (`jarvisd/claude.py`) asks for that and tells the model to skip done, cosmetic, optional or
  already covered items. The deterministic fallback scores action needed (overdue task, deadline
  or blocker words, CI failing, PR review, commits) instead of taking the newest threads.
- Attention: deterministic, `Nothing broken.` or one line per anomaly (CI failing, failed jobs,
  daemon crashes, unclean exits, overdue task, held backlog over 20, breaker open, invalid config).
- Still open replaces the Brain section: one key per thread (`common.norm_key`), RECENT.md and
  session copies collapsed in `collectors/brain.py` with the session slug kept for grouping; stale,
  resolved, cosmetic, "No active work" and wikilink-only lines never print; cap 10 with a count line.
- Decided yesterday: window dates only, rationale cut, cap 10, omitted when empty.
- Repos: a line only for a repo with commits or an ahead/behind count, dirty repos on one
  `Uncommitted only` line, `Quiet: N repos.`, GitHub lines only for PRs or failing CI, the unread
  repos as a count by state instead of sixteen names.
- System: one `All green` line, or one line per anomaly; `collectors/system.py` decodes
  `LastTaskResult` (`TASK_RESULTS`) and adds the `daemon_crashes`, `config_invalid` and disk facts.
  Held back is one line. The "No ClickUp call was made (v1)" suffix is gone.
- `jarvis run-digest --dry-run` now also renders the note in memory (`result["note"]`).
- Goldens regenerated deliberately for grammar 2; `tests/test_render.py` checks every line against
  the contract regexes.

## 1.2.0 - 2026-10-06

- Reminders view in the hub (`/reminders`, `HubData.reminders`): due dates of confirmed and open proposals, grouped by
  how late they are. Read only, no tracker call, nothing pushed. Tested offline (`tests/test_hub_reminders.py`).

The work hub moves from a viewer to a cockpit that can turn a proposal into a task, and only on a
human decision. Each entry says what was run. Nothing below spent money: no paid proposals run was
made and ClickUp was never called. The hub is no longer GET only: the Inbox adds three POST routes.
A review pass after the build found the problems listed under Fixed; they are fixed in this release.

### Added: built and tested offline
- Q1, task proposals (`jarvis propose`, `jarvis proposals`, `jarvisd/propose.py`, `docs/proposals.md`):
  one capped Claude call turns the work items into at most `[propose].max_proposals` proposals under
  `state/proposals/`. It is anchored on the latest complete digest run, but the items are collected and
  gated again when it runs (the digest manifest holds counts and hashes, not item ids). Same gates, sealed payload and
  isolated argv as the digest; held items appear as ids only; every evidence id is checked against
  what was sent; the 20 newest rejected proposals are sent back as negative examples. Off by
  default. Only the dry run was ever executed against real state.
- Q2, tracker adapters (`jarvis tracker check`, `jarvisd/tracker.py`): a `markdown` adapter that
  appends to `raw/jarvis/confirmed-tasks.md` through the single vault writer, and a `clickup`
  adapter that creates one task through the ClickUp REST API with a token read from a named
  environment variable. A `dry_run` switch builds the exact request and shows it to you instead of
  sending it, and every attempt is audited. The ClickUp adapter has only met a local stand-in server.
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
- The daemon now enqueues the proposals job right after a complete digest, and again on every tick,
  once per date (`jarvisd/daemon.py`, `propose.reconcile_proposals`). It is inert while
  `[propose].enabled` is false, which is the shipped default.
- The hub's POST check compares `Origin` with the `Host` header instead of a fixed loopback address.
- The Projects view shows days idle with a plus sign when the repository has no activity in any digest
  on file, and the Ledger and its monthly sum include the proposals runs.
- Versions agree everywhere: package, `jarvis.toml [meta]`, citation and changelog are 1.2.0.

### Fixed
- An ambiguous tracker outcome (timeout, dropped connection, 5xx, an answer that is not a task, a
  crash between the call and the save) no longer says "still open" and invites a second click that
  would duplicate the task. The Inbox writes a `<id>.attempt` marker before every send, refuses a plain
  second confirm while it exists, and offers an explicit Confirm anyway (`--confirm-anyway`).
- Confirm now gates the text again: the title, project and rationale as they will be sent go through
  the tier gate, an evidence id that is held stops the confirm, and an edited title or project is
  scanned. Links, images and HTML in the model's text are reduced to their words, in the proposals
  job and in both tracker adapters, so a remote image cannot make Obsidian fetch a URL.
- A retried proposals job no longer rewrites a proposal file that exists, so it cannot undo a rejection.
  The proposals window is at least `window_hours_default` hours even after a forced digest rerun. The
  model is shown the live proposals so it does not re-propose standing work in new words.
- The hub no longer answers 500 to a form with too many fields, and its Origin check works from
  `http://localhost` and through `tailscale serve`, where every Inbox button used to be refused.
- A ClickUp dry run needs no token, and its audit record keeps ids, size and hash instead of the
  request text. `proposal_created`, `proposal_confirmed`, `proposal_confirm_failed` and
  `proposal_rejected` carry the digest id as `digest_run_id` and no longer overwrite the audit
  envelope's `run_id`.
- The tracked `jarvis.toml` no longer carries `[hub].stale_days`, which a daemon still running 1.1.0
  rejected, stopping its job ticks. The default (14) lives in code.
- The Projects stale badge can fire for a repository with no recorded activity.
- Documentation: the README test badge, status row, command table and next steps, the hub and
  proposals pages (Origin, the audit contents, transitive imports, the terminal door, the stale
  definition), and the example local file's `[hub.task_projects]` key.

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
