# jarvisd v1, option 2: implementation plan

Design: `docs/v1-design.md` (section numbers below refer to it; spec sections are marked §).
This plan is the historical build order of the first slice; what exists now is in the `README.md`
status table.
Thirteen ordered tasks. Each is sized so one implementer agent finishes it in one pass,
writes the named test first, and stops when the acceptance criteria hold. Paths are
relative to `C:/Users/<owner>/jarvis` unless absolute.

Rules for every task:

- Test first. The "test to write first" is written and seen failing before the module.
- Stdlib first. Only `pydantic` and `APScheduler` are allowed imports beyond the stdlib and
  pytest (design section 4). No `httpx`, no `claude-agent-sdk`, no `pydantic-ai`.
- Style: `from __future__ import annotations`, full type hints, `main(argv) -> int`, small
  functions, comments say why, limitations stated. No em dashes or en dashes anywhere, in
  code, comments, docs or output. Exact file paths. UTF-8 without BOM.
- Never touch `bin/`, `srt-settings.json`, `docs/sandbox-policy.md`, `docs/phase0-runbook.md`
  or existing keys of `jarvis.toml`. New `jarvis.toml` tables are appended in their own
  commit and flagged for the owner (the file is human-owned).
- Synthetic fixtures only. No brain content, no work project data, no NDA material in
  tracked files (design D10).
- Run `python -m pytest -q` and `python bin\watchdog.py --self-test` before declaring done.

Dependency graph:

```
T1 -> T2 -> T5 (vault) ---------------------------+
T1 -> T3 (tier) -> T4 (router, dispatch) ---------+--> T9 (digest, notify) -> T10 (daemon, cli) -> T11 (deploy, docs) -> T12 (live, first run) -> T13 (ClickUp, stretch)
T1 -> T2 -> T6 (state, jobstore, reconcile) ------+
T2 + T4 -> T7 (claude client, fake claude) -------+
T3 -> T8 (collectors, render) --------------------+
```

---

## T1. Scaffold, config loader, models, common helpers, jarvis.toml append

Files:
- `pyproject.toml`, `requirements.lock`, `deploy/setup-venv.ps1`
- `.gitignore` (add `state/`, `hub/`, `*.tmp`)
- `jarvis.toml` (append only, separate commit)
- `jarvis.local.toml.example`
- `jarvisd/__init__.py`, `jarvisd/__main__.py`, `jarvisd/common.py`, `jarvisd/config.py`,
  `jarvisd/models.py`
- `jarvis.cmd`
- `tests/conftest.py`, `tests/test_config.py`, `tests/test_models.py`, `tests/test_common.py`

Test to write first: `tests/test_config.py::test_real_toml_loads_with_defaults` (loads the
repo's `jarvis.toml`, asserts `cfg.digest.work_metadata_to_claude is False`,
`cfg.claude.model == "sonnet"`, `cfg.router.adapter == "stub"`, `cfg.sha256` is 64 hex) and
`::test_missing_gates_table_raises` (a toml without `[gates]` raises `ConfigError`).

Work:
- `deploy/setup-venv.ps1` creates `.venv` with the Python 3.12 at
  `C:\Users\<owner>\AppData\Local\Programs\Python\Python312\python.exe`, installs
  `pydantic>=2.7,<3`, `APScheduler>=3.10,<4`, `pytest`, writes `requirements.lock` with
  exact pins. Verify `pip install APScheduler` works before pinning.
- Append to `jarvis.toml`: `[daemon]` (`tick_seconds = 120`, `heartbeat_seconds = 30`,
  `state_dir`), `[digest]` (`run_at = "06:30"`, `window_hours_default = 36`,
  `window_hours_max = 72`, `max_payload_bytes = 40000`, `max_item_chars = 600`,
  `write_session_note = false`, `clickup_enabled = false`, `dirty_ignore = [...]`,
  `work_metadata_to_claude = false`), `[claude]` (`binary = ""`, `model = "sonnet"`,
  `timeout_seconds = 180`, `max_budget_usd = 0.50`, `daily_budget_usd = 2.00`,
  `daily_calls = 6`, `isolation_overhead_tokens = 3000`, `archive_payloads = false`,
  `required_flags = [...]`), `[local]` (`enabled = false`, `backend = ""`), `[notify]`
  (`adapter = "toast"`, `toast_script`), `[retention]`, `[queue.classes.background_batch]`
  (`timeout_s = 1800`, `local_wait_s = 600`), `[router.stub]` (`confidence = 0.0`,
  `rules.financial`, `rules.client_facing`, `rules.irreversible`, `rules.work_prod` regex
  lists) and one new key `adapter = "stub"` under `[router]`. Repos are NOT added here.
- `jarvis.local.toml.example`: `[digest].repos = [{name, path, work, counts_only}]` with
  synthetic names, `[gates].sensitive_terms = []`, `[gates].sensitive_path_globs` (appended),
  `[digest].work_metadata_to_claude`, `[hygiene].deny_substrings = []`.
- `config.py`: `tomllib` load, deep merge of `jarvis.local.toml` (lists under `[gates]`
  append, scalars override), pydantic `Config` with `extra="forbid"` on new tables and
  `extra="ignore"` on Phase 0 tables, `sensitive_globs()` merges the hardcoded floor,
  `confidence_threshold` must be a float (matches the watchdog self-test).
- `models.py`: `Item`, `WithheldItem`, `RouterDecision` (exactly `category, sensitive,
  importance, confidence, needs_tools, language, reason`, `extra="forbid"`, confidence in
  [0, 1], importance in `low|med|high`), `TierHit`, `GateResult`, `Job` (schema in design
  section 5), `CollectResult`, `DigestSummary`, `Attention`, `RunManifest`, `ClaudeReply`.
- `common.py`: `now_utc`, `iso`, `parse_iso`, `local_now`, `sha256_hex`, `canonical_json`,
  `strip_dashes`, `short_id`.
- `conftest.py`: `tmp_vault` (brain tree with `telos/sensitive/canary.md` holding
  `JARVIS-CANARY-7f3a`, `telos/`, `notes/`, `raw/jarvis/`, `sessions/`,
  `session-checkpoints/{processed,from-old-machine}/`, a synthetic `RECENT.md`), `tmp_cfg`
  (a `Config` pointing every path at `tmp_path`), `FakeClock`.

Acceptance:
- `pytest tests/test_config.py tests/test_models.py tests/test_common.py` passes.
- `RouterDecision` rejects an extra field, confidence 1.5 and importance `urgent`.
- `strip_dashes` removes U+2014 and U+2013; `short_id` is stable across calls.
- `python -m jarvisd --help` exits 0; `jarvis.cmd --help` exits 0.
- `python bin\watchdog.py --self-test` still reports all checks passing after the append.
- The daemon code contains no write path to `jarvis.toml` (grep in a test).
- No new file contains U+2014 or U+2013.

Depends on: none.

---

## T2. fsio and hash-chained audit log

Files: `jarvisd/fsio.py`, `jarvisd/audit.py`, `tests/test_fsio.py`, `tests/test_audit.py`

Test to write first: `tests/test_audit.py::test_tamper_detected_at_exact_seq` (emit 50
records, flip one byte in record 23, `verify` returns `(False, 23)`) and
`tests/test_fsio.py::test_atomic_write_retries_then_fallback` (monkeypatched `os.replace`
raising `PermissionError` twice then succeeding, retries counted, no `.tmp` left).

Work:
- `fsio.atomic_write_text(path, text, *, retries=6)`: temp `.<name>.<pid>.tmp` in the same
  directory, UTF-8 no BOM, flush and fsync, `os.replace`, backoff 0.2, 0.5, 1, 2, 4, 8 s on
  `PermissionError`, raises `FileBusy` after the last retry, always removes the temp.
  `FileLock(path)` with `msvcrt.locking`. `append_line_locked`.
- `audit.AuditLog(path, max_bytes=20_000_000, keep_days=180)`: records `ts, event, seq,
  prev, h, run_id, job_id, pid, ver, ...fields`; `h = sha256(prev + canonical_json(record
  without h))`; fsync per line; redaction guard (truncate strings over 500 chars, drop keys
  `prompt, content, text, body, title`); `emit` never raises (stderr warn like
  `bin/watchdog.py` JsonlLog, plus a `.fallback` file), mirrors to stdout only when
  `sys.stdout` is not None; `head()`; `records(since, events)` reads rotated files
  tolerant of `utf-8-sig`; `cost_on(date)` sums `claude_call.total_cost_usd`;
  `rotate_if_due()` by size or month, first record of the new file carries the old head;
  `verify(paths)` across files.

Acceptance:
- Both test files pass. Chain verifies on a fresh log; a one-byte edit is reported at the
  right seq; rotation at a tiny `max_bytes` keeps `seq` and `prev` continuous and `verify`
  passes across files; `records()` reads rotated files; `cost_on` sums by local date.
- Four threads emitting concurrently yield unique contiguous `seq`.
- A write to an unwritable path does not raise and leaves a `.fallback` file.
- Output has no BOM; `ts` and `event` are the first keys.
- `FileLock` denies a second holder in another process (subprocess test).

Depends on: T1.

---

## T3. Tier gate: canonical paths, hardcoded floor, text scan, safe_read_text

Files: `jarvisd/tier.py`, `tests/test_tier.py`

Test to write first: `tests/test_tier.py::test_adversarial_paths_all_hit` (parametrized
table) and `::test_safe_read_text_never_opens_withheld_file` (an `open` tracer proves the
canary file is never opened).

Work:
- `canonical(path)`: reject NUL and `::$DATA`, strip `\\?\`, expand 8.3 names with
  `GetLongPathNameW` on the existing prefix, `os.path.realpath`, `normcase`, forward
  slashes, reject any remaining `..`; any `OSError` yields `TierHit("sensitive",
  "tier_error_fail_closed")` at the caller.
- Hardcoded floor: under `brain/telos/` (whole tree), under `brain/notes/`, any component
  equal to `sensitive` after casefold and trailing dot or space strip. Merged with
  `[gates].sensitive_path_globs` and the local extra globs; config can only add.
- `path_hit`, `text_hit` (frontmatter `tags:` with any `[gates].sensitive_tags`,
  `sensitive: true`, inline `#tag`, literal `telos/sensitive`, `[gates].sensitive_terms`
  case-insensitive with accents folded; code is `term:<index>`, never the term), `item_hit`
  (all `paths`, `origin`, path-like `meta` values, title, text, tags, the tag
  `derived_from_sensitive_session`), `safe_read_text(path, cfg, roots, max_bytes)` (refuses
  paths outside `roots` without opening; path hit returns `WithheldItem` without opening;
  reads `utf-8-sig`; whole-file text hit returns `WithheldItem`), `assert_clean(prompt)`.

Acceptance (table rows, each must hit): `TELOS/Sensitive/x.md`, `telos\sensitive\x.md`,
`telos/sensitive./x.md`, `telos/sensitive /x.md`, `SENSIT~1` short name (created with
`fsutil 8dot3name` or skipped with reason), `..\telos\sensitive\x.md`,
`brain/sessions/../telos/sensitive/x.md`, `\\?\C:\...\telos\sensitive\x.md`,
`telos/sensitive/x.md::$DATA`, junction `brain/sessions/link -> telos/sensitive` (`mklink
/J` in `tmp_path`), symlink (skipped without privilege), a path with NUL, an unresolvable
path, a path outside the roots (never opened). Text rows: frontmatter `tags: [x, medical]`,
inline `#private`, `sensitive: true`, wikilink `[[telos/sensitive/health]]`, commit subject
`fix #sensitive`, mixed case `TAGS:`, a configured term with different accents and case.
Negative rows: plain prose containing the word `private`, a path under `brain/raw/jarvis`.
A raised exception inside a check yields a fail-closed hit. The hit code of a term match
does not contain the term. Removing the globs from the toml still withholds by floor.

Depends on: T1.

---

## T4. Router contract, StubRouter, dispatch gates, GatedPayload

Files: `jarvisd/router.py`, `jarvisd/dispatch.py`, `tests/test_router.py`,
`tests/test_dispatch.py`

Test to write first: `tests/test_dispatch.py::test_gate_truth_table` (at least 24 rows over
tier hit, `router.sensitive`, importance, confidence above or below 0.72, local state in
`not_installed|unavailable|up`) and `::test_router_not_called_for_tier_hits` (fake router
with a call counter).

Work:
- `router.py`: `Router` protocol (docstring: routers must be local; a Claude-backed router is
  forbidden), `StubRouter` per design section 6, `ROUTERS`, `build_router(cfg)` raising
  `ConfigError` on an unknown adapter.
- `dispatch.py`: `LocalBackend` protocol and `LOCAL_BACKENDS = {}`, `local_state(cfg)`
  returning `not_installed` when `[local].enabled` is false, `unavailable` when enabled with
  no healthy backend, `up` otherwise; pure `decide()` exactly as in design section 6;
  `run_gates(items, router, cfg, local, audit)` that computes the tier hit first, calls the
  router only when the hit is None, emits one `gate_decision` audit record per item;
  `GatedPayload` frozen model whose constructor requires a module-private token;
  `clear_for_claude(items, results, cfg)` (drops non-claude routes, re-runs `item_hit`,
  applies the size cap by priority with an `over_cap` list, builds the data block without
  paths, strips `</data>`, `sha256`, `assert_clean`), `PayloadBlocked`.

Acceptance:
- Truth table passes with ordering proofs: tier beats importance beats confidence; a router
  returning `sensitive = False` never clears a tier hit; a router returning `sensitive =
  True` holds; the stub always yields `route = claude`, `degraded = False`, `local_tier =
  not_installed`; a fake confident router with `local_state = unavailable` yields
  `degraded = True`; with `up`, sensitive items route `local`.
- `work` items are `policy` held unless `work_metadata_to_claude` is true.
- `StubRouter` output validates, has exactly the 7 fields, confidence 0.0, importance rules
  and language heuristic (`en`, `fr`, `ar-darija-latin`, `other`) behave on samples.
- `GatedPayload(...)` built outside `dispatch` raises; `clear_for_claude` drops held items,
  caps size, strips `</data>`, raises `PayloadBlocked` when a canary is injected after
  gating. The audit record contains no title or text.
- Registering a fake router under a new name and selecting it by config needs no change in
  `dispatch.py` (test does it).

Depends on: T3.

---

## T5. Vault writer

Files: `jarvisd/vault.py`, `tests/test_vault.py`, `tests/test_write_locations.py`

Test to write first: `tests/test_vault.py::test_denied_targets` (parametrized over `telos`,
`notes`, `Documents/<work>`, `raw/x.md`, `raw/jarvis/../x.md`, `sessions/x.md`, a junction
under `raw/jarvis` pointing at `notes`, an absolute path elsewhere, `jarvis.toml`) and
`tests/test_write_locations.py::test_only_named_modules_write_files` (AST scan).

Work: `VaultWriter(cfg, audit)` per design section 9: `write_raw(name, text, job_id)`,
`write_session(slug, text, job_id)` (disabled by config), relative names only, canonical
resolution, realpath parent equality, generator marker check before replace, `fsio`
atomic write, fallback name `<stem>-r2.md` after `FileBusy`, `vault_intent` and
`vault_write` audit, `VaultWriteDenied` plus `vault_violation` audit.

Acceptance:
- Allowed: `raw/jarvis/digest-2026-10-06.md`, `raw/jarvis/candidates/x.md`,
  `sessions/jarvis-x.md` (when enabled). Every denied row raises and audits.
- An existing file lacking `generator: jarvisd` is never overwritten.
- A destination held open without sharing (Windows handle in the test) triggers the patched
  backoff then the `-r2` name with `fallback_used = True`; no temp file remains after
  success or failure; temp is a hidden dotfile in the same directory; result `sha256`
  equals the file hash; no BOM; LF.
- The AST test fails if any module outside `vault.py`, `audit.py`, `jobstore.py`,
  `state.py`, `fsio.py`, `claude.py`, `cli.py` opens a file for writing or calls
  `os.replace`, `shutil.move`, `Path.write_text`; only `vault.py` references the vault root
  for writes.

Depends on: T2.

---

## T6. Operational state, JSON job store, held references, reconcile

Files: `jarvisd/state.py`, `jarvisd/jobstore.py`, `jarvisd/scheduler.py` (the pure
`reconcile` and `due_at` only; the APScheduler host comes in T10), `tests/test_state.py`,
`tests/test_jobstore.py`, `tests/test_reconcile.py`

Test to write first: `tests/test_reconcile.py::test_thousand_calls_two_threads_one_job`
and `tests/test_state.py::test_reservation_survives_restart`.

Work:
- `state.StateStore(dir)`: budget ledger (`reserve(purpose, usd)` refusing at
  `daily_budget_usd` and `daily_calls`, `settle`, date rollover, `snapshot`), breaker
  (`is_open`, `record_success`, `record_failure`, `trip(reason, requires_reset)`, `reset`,
  cooldown 60 min, half-open), watermark (`get`, `advance` only forward), `heartbeat`,
  `previous_exit_clean`, `mark_clean_shutdown`, `killed`, `paused`,
  `acquire_daemon_lock` raising `AlreadyRunning`. All writes atomic JSON in `state/`.
- `jobstore.JobStore(root)`: refuses a root under `brain/` or with a `.stfolder` ancestor;
  `enqueue` with `O_EXCL` on `pending/<id>.json` and `exists` across all state dirs;
  `claim_next(now)` honouring `not_before` and age order; `update`, `complete`, `retry`,
  `fail`; `hold(ref, job_id)` writing `held/<item_id>.json` with no title or text and
  merging `digest_ids` on repeat; `held(date)`; `recover_running()`; `expire_held(now)`;
  `counts()`; `prune(...)`; directory wins over the `state` field, mismatch audited.
- `scheduler.reconcile(now, cfg, state, store, audit)` per design section 10 steps 2 to 7
  (step 1, the kill check, lives in the daemon tick); `due_at(cfg, day)`.

Acceptance:
- Budget refuses at the USD cap and the call cap; a reservation persists across a new
  `StateStore` on the same dir; `settle` replaces it; rollover resets at the local date.
- Breaker opens after 3 failures, persists, honours cooldown and half-open, requires reset
  when tripped with `requires_reset`.
- Second `acquire_daemon_lock` in another process raises; lock frees when the holder dies.
- `enqueue` is idempotent across all five state dirs and safe under 8 threads (exactly one
  winner); state moves are atomic; `recover_running` preserves attempts; a job with
  `attempts >= max_attempts` goes to `failed`; a corrupted job file is moved to `failed`
  with a readable error instead of crashing the loop; held files contain only the allowed
  keys (schema test); `expire_held` removes 14-day-old references.
- Reconcile table with `FakeClock`: before 06:30 none; at 06:31 one; already present none;
  clock jump 23:00 to 09:40 one `catchup`; three-day gap one job with older pending moved to
  `failed` as `coalesced_into`; stale `RECENT.md` waits until due plus 90 min; tz change
  mid-day never double fires one date.

Depends on: T2.

---

## T7. Claude client, fake claude binary, isolation checks

Files: `jarvisd/claude.py`, `tests/fakes/fake_claude.py`, `tests/test_claude.py`,
`tests/fixtures/claude_result_ok.json`

Test to write first: `tests/test_claude.py::test_argv_golden_and_env_allowlist` (exact
argv list from design section 7, prompt absent from argv and present on the fake's recorded
stdin, env contains none of `ANTHROPIC_*` or `CLAUDE_*`) and `::test_rejects_non_payload`.

Work:
- `fake_claude.py`: records argv, env keys, cwd and stdin bytes to `FAKE_CLAUDE_LOG`,
  emits scenario JSON by `FAKE_CLAUDE_SCENARIO` (`ok`, `timeout` via sleep, `429`, `401`,
  `invalid_json`, `isolation_violation` with 20k cache tokens, `hang`), also answers
  `--version` and `--help` (listing the required flags) so preflight runs offline.
- `claude.ClaudeClient(cfg, audit, state, runner=None, enabled=False)`: `preflight()`,
  `build_argv`, `child_env` (allowlist), `complete(payload, purpose, attempt)` doing, in
  order: `TypeError` unless `GatedPayload`; `ClaudeUnavailable("disabled")` when not
  enabled; breaker check; network readiness probe (injectable); `state.budget.reserve`;
  `claude_intent` audit; snapshot of `session-checkpoints/` and `sessions/` listings; spawn
  with stdin, `cwd = state/claude-cwd`, `CREATE_NO_WINDOW`; wait loop with 1 s polls
  checking `state.killed()`, `taskkill /T /F` on timeout; parse; token tripwire;
  side-effect snapshot diff; `settle`; `claude_call` audit; map failures to
  `ClaudeUnavailable(kind, retryable)`; optional payload archive when
  `[claude].archive_payloads`.

Acceptance:
- Golden argv matches exactly, including the empty-string values for `--setting-sources`
  and `--tools`; `--max-turns` is appended only when the fake `--help` advertises it.
- Scenarios: `ok` parses tokens, cost, `session_id`; `timeout` kills the process tree
  within 2 s of the deadline and reports `kind = timeout`; `429` not retryable, breaker
  failure recorded; `401` opens the breaker with `requires_reset`; `invalid_json` is
  `bad_json`, not retryable; `isolation_violation` discards output, audits
  `isolation_anomaly`, opens the breaker; a fake that creates a file in the tmp
  `session-checkpoints/` triggers `isolation_breach` and the breaker.
- A `state/KILL` created during a `hang` scenario terminates the child and raises
  `kind = killed`.
- `claude_intent` is written before spawn and the reservation exists in `budget.json` even
  if the test kills the client between intent and result.
- Budget refusal emits `budget_refused` and makes no spawn.
- The `claude_call` record holds no payload text; the `payload_sha256` matches the sealed
  payload. Fenced JSON in `result` is accepted; unknown ids are dropped and counted.

Depends on: T2, T4.

---

## T8. Collectors (brain, task, git, system) and the renderer

Files: `jarvisd/collectors/__init__.py`, `jarvisd/collectors/brain.py`,
`jarvisd/collectors/task.py`, `jarvisd/collectors/git.py`,
`jarvisd/collectors/system.py`, `jarvisd/render.py`, `tests/test_collectors_brain.py`,
`tests/test_collectors_git.py`, `tests/test_collectors_system.py`, `tests/test_render.py`,
`tests/golden/digest_complete.md`, `tests/golden/digest_degraded.md`,
`tests/golden/digest_empty.md`, `tests/golden/digest_all_held.md`,
`tests/fixtures/schtasks_info_nightly.json`

Test to write first: `tests/test_collectors_brain.py::test_sensitive_session_withheld_whole_and_never_opened`
and `tests/test_render.py::test_golden_complete` (then the other goldens).

Work:
- `collectors/__init__.py`: `Collector` protocol, `compute_window`, `run_collectors`
  converting exceptions and timeouts into `CollectResult(ok=False, error=...)`.
- `brain.py`, `task.py`, `git.py`, `system.py` per design section 8. Collectors read only
  through `tier.safe_read_text` (AST test added to `test_write_locations.py`: no `open(`
  or `read_text` in `collectors/`). `git.run_git` with the subcommand allowlist.
- `render.py`: `render_digest(ctx)` producing design section 9 exactly, section registry
  (`SECTIONS` dict keyed by name, order from `[digest].sections` with a sane default),
  deterministic "Start here" fallback order, `strip_dashes` on every Claude string, the
  `## Open threads` heading never emitted.

Acceptance:
- Brain: `RECENT.md` bullets become `brain_thread` and `brain_decision` items with stable
  ids and stale flags; sessions newer than the window become `brain_session` items,
  `jarvis-*` excluded; a session with `tags: [sensitive]` is withheld whole without being
  opened (tracer) and same-date `RECENT.md` bullets get `derived_from_sensitive_session`;
  checkpoints counted excluding `processed/` and `from-old-machine/` and never opened;
  `telos/`, `notes/`, `insights/`, `raw/` never opened (tracer); stale `RECENT.md` reported.
- Task: opens exactly the five named files, handles missing files, `due_state` correct.
- Git: on temp repos, commits, dirty and untracked counts, ahead/behind parsed; noise
  filtered; `counts_only` repos emit no subjects or file names; non-repo and missing paths
  yield facts not failures; recorded argv contains only allowlisted verbs plus
  `--no-optional-locks`; env has `GIT_OPTIONAL_LOCKS=0`; `.git/index` mtime unchanged after
  collection; `fetch` raises `GitNotAllowed`; a tagged commit subject is withheld.
- System: numbers from a synthetic audit and `killswitch.jsonl`, `schtasks` JSON fixture
  parsed with a fake runner, degrades to "unavailable" text on failure; items carry
  `render = deterministic`.
- Render: four goldens match; frontmatter keys as designed; no table pipes; no U+2014 or
  U+2013; sensitive-held items appear only as count plus ids; an empty window renders the
  explicit "Nothing changed overnight" line; the fixed GitHub line is present; adding a
  section class to the registry in a test appears without touching `render.py`.

Depends on: T3 (and T4 for `GateResult` shapes).

---

## T9. Digest pipeline and notifier

Files: `jarvisd/digest.py`, `jarvisd/notify.py`, `deploy/notify-jarvis.ps1`,
`tests/test_digest_e2e.py`, `tests/test_notify.py`

Test to write first: `tests/test_digest_e2e.py::test_canary_never_leaves` (canary planted in
`telos/sensitive`, in a tagged session, in a tagged commit subject and in a checkpoint cwd;
full pipeline with the fake claude; canary absent from recorded stdin, argv, env, audit,
toast and digest; held count equals planted count).

Work:
- `digest.run_digest_job(job, deps, *, mode)` per design section 3 steps 1 to 8: collect,
  gate, seal, summarize (one call), render, write, notify, finish (watermark advance only
  after a successful write, `state/runs/<job_id>/run.json` manifest, held references via
  `jobstore.hold`, job aggregates `tier`, `importance`, `confidence`, `sensitive`,
  `degraded`). Retryable Claude failure on a non-final attempt raises `Retry`; final attempt
  or past deadline renders deterministic with `claude_status` and completes the job. Budget,
  auth, breaker and `PayloadBlocked` paths are immediate deterministic fallbacks.
- `notify.py`: `Notifier` protocol, `ToastNotifier(script)` with the exact argv list, 15 s
  timeout, no shell, never raises; `NullNotifier`; `digest_message(...)` from integers and
  fixed phrases only; `build_notifier(cfg)`.
- `deploy/notify-jarvis.ps1`: copy of `C:/Users/<owner>/.claude/hooks/notify.ps1` with
  AppUserModelId `JARVIS`, UTF-8 without BOM.

Acceptance:
- Canary test passes. Note written to `raw/jarvis/digest-<date>.md` with
  `generator: jarvisd`; watermark advances only on success; first Claude failure raises
  `Retry` and leaves no note; third failure or a past deadline writes the deterministic
  note with `status: degraded_no_llm` and completes the job; budget, auth and
  `PayloadBlocked` paths are non-retryable fallbacks with the loud line; exactly one
  notification per job with the designed message variants and no item text; a directory
  snapshot shows no new files in `session-checkpoints/` or anywhere outside the allowed
  vault paths; audit has one `gate_decision` per item, one `claude_intent` plus one
  `claude_call` per call, and no record contains title or text; `run.json` has stage
  statuses, counts, cost, paths and hashes; held references exist in `queue/held/` for each
  withheld item; a second run for the same date is a no-op without `--force`.
- `ToastNotifier` builds the exact argv, caps the message at 180 chars, returns `ok=False`
  on a missing script, non-zero exit or timeout without raising.

Depends on: T5, T6, T7, T8.

---

## T10. Daemon loop, APScheduler host, CLI, self-test

Files: `jarvisd/scheduler.py` (add `SchedulerHost`), `jarvisd/daemon.py`,
`jarvisd/cli.py`, `jarvisd/selftest.py`, `jarvisd/__main__.py` (excepthook, faulthandler),
`tests/test_daemon.py`, `tests/test_cli.py`, `tests/test_crash_trail.py`

Test to write first: `tests/test_daemon.py::test_serve_three_ticks_runs_todays_job_once`
(fake clock, `max_ticks=3`) and `tests/test_cli.py::test_run_digest_default_spends_nothing`
(fake claude call count is zero without `--claude`).

Work:
- `daemon.build_deps(cfg)` used by both the daemon and the CLI; `serve(cfg, *, task_mode,
  max_ticks=None)`: lock, startup audit with `previous_exit`, `KILL` check, `recover_running`,
  prune, preflight, heartbeat thread, `SchedulerHost` (BackgroundScheduler, MemoryJobStore,
  tzlocal timezone, cron `[digest].run_at`, interval `tick_seconds`, daily housekeeping,
  monthly `disk_check`; all jobs call idempotent functions; `coalesce=True`,
  `max_instances=1`); `tick(deps, now)` runs the kill and pause checks, task-disabled check
  in task mode (`schtasks /query /tn JarvisDaemon /fo CSV /nh` via an injectable runner),
  `reconcile`, claim and run; unhandled exceptions inside a job are audited as `job_failed`
  and the loop continues; no stdout use under pythonw; no TCP listener.
- `cli.main(argv)` implementing every subcommand in design section 11 with the documented
  exit codes; `run-digest` defaults to no Claude.
- `selftest.run(cfg, live=False)` with the checks listed in design section 11.

Acceptance:
- Daemon: three ticks enqueue and run once; second instance exits without running; `KILL`
  returns 3 and a restart with `KILL` present returns 3 immediately; `PAUSE` stops enqueue
  and claim while the heartbeat updates; a Disabled task (fake runner output) causes a clean
  exit with `killswitch_seen`; dev mode never spawns the fake claude; `heartbeat.json` is
  rewritten atomically; `daemon_start` records `mode`, `account`, `claude_cli_version`,
  `local_tier`, `config_sha256`; socket listing shows no new listener.
- Crash trail: daemon in a subprocess with scenario `hang`, `taskkill /F` after
  `claude_intent` appears, restart; audit shows `claude_intent` without `claude_call`, then
  `unclean_previous_exit`, `job_recover`; reservation counted; chain verifies; excepthook
  writes `daemon_crash`.
- CLI: every subcommand parses; `status` exits 1 when stopped and `--json` has the
  documented keys including `local_tier: not_installed`; `run-digest --dry-run` prints the
  payload and held list, spawns nothing, writes nothing; `run-digest --claude --no-notify`
  with the fake binary writes a note in the tmp vault and audits `mode = manual`; `wrong`
  writes `state/corrections.jsonl` and an audit `correction`, unknown id exits 2, `--leak`
  opens the breaker; `held` resolves an id; `pause` and `resume` toggle; `audit verify`
  returns 1 on a tampered chain; `breaker reset` closes it; `self-test` prints `[PASS]` or
  `[FAIL]` lines and exits 1 on any FAIL.

Depends on: T9.

---

## T11. Deploy script, docs, kill-switch patch proposal, hygiene tests, README section

Files: `deploy/register-jarvisd-task.ps1`, `docs/killswitch-v1-patch.md`,
`docs/v1-operations.md`, `README.md` (append a "v1 option 2" section only; the Phase 0
table stays), `tests/test_repo_hygiene.py`

Test to write first: `tests/test_repo_hygiene.py::test_no_em_dashes_in_tracked_files` and
`::test_gitignore_covers_runtime_dirs`.

Work:
- `register-jarvisd-task.ps1`: the exact block from design section 10, idempotent (`-Force`),
  `-Unregister` and `-WhatIf`, prints `Get-ScheduledTaskInfo JarvisDaemon` at the end,
  UTF-8 without BOM, same style as `bin/*.ps1`.
- `docs/killswitch-v1-patch.md`: proposal only (unified diff in a fenced block) for
  `bin/kill-switch.ps1` to accept `-TargetAccount <owner>` plus a command-line marker
  `jarvisd serve --task`, and for the `JarvisKillSwitch` task arguments. States that `bin/`
  is Phase 0 and the change needs the owner's commit and a hostile-sim rerun.
- `docs/v1-operations.md`: start, stop, pause, first-run checklist, reading the audit, the
  incident runbook, the local-tier plug-in recipe (copied from design section 15), known
  gaps (design section 16).
- README: append a `## v1 option 2 (phase 2 slice)` section after the Phase 0 section
  stating: runs as `<owner>` and why, observe-only, not sandboxed, kill paths that do
  and do not apply, audit tamper-evident not tamper-proof, local tier is a stub with
  `not_installed` semantics, no ntfy, GitHub not collected, ClickUp stretch, public-repo
  hygiene, and that v1 is option 2 so the phase table is not renumbered. The existing
  one-line pointer under "Phase 0 state" stays.
- Hygiene tests: no U+2014 or U+2013 in tracked text files under `jarvisd/`, `tests/`,
  `docs/`, `deploy/`; `.gitignore` covers `state/`, `queue/`, `logs/`, `.venv/`,
  `jarvis.local.toml`, `*.tmp`; no tracked file contains an absolute path under
  `brain/telos/sensitive`; repo root has no `CLAUDE.md` or `.claude/`; if
  `state/hygiene-denylist.txt` exists no tracked file matches any of its lines (skipped
  with a message otherwise); `git diff --stat -- bin/` is empty.

Acceptance:
- `pytest tests/test_repo_hygiene.py` passes; the whole suite passes; `bin/` unchanged.
- `powershell -File deploy\register-jarvisd-task.ps1 -WhatIf` prints the plan without
  registering.
- README diff touches only the appended section; the Phase 0 table is byte-identical.
- Docs contain no dashes other than hyphens and reference spec sections by number.

Depends on: T10.

---

## T12. Live smoke, owner review, first real digest, task registration, kill-switch contract

Files: `tests/live/test_claude_isolation.py`, `jarvis.local.toml` (gitignored, written by
the owner with the agent's help), `docs/v1-operations.md` (append the run log)

Test to write first: `tests/live/test_claude_isolation.py` (marker `live`): isolated PONG
call has `input + cache_creation + cache_read < 2000` and `total_cost_usd < 0.005`; a
`stream-json --verbose --include-hook-events` probe shows `tools []`, `mcp_servers []`,
`slash_commands []`, `skills []` and no hook events; the file count in
`C:/Users/<owner>/brain/session-checkpoints/` is unchanged; empty-string arguments
survive the resolved binary.

Human-run checklist, each step logged in `docs/v1-operations.md` with date and result:

1. Create `jarvis.local.toml` from the example: the repo list (work repos under
   `C:/Users/<owner>/Documents/<work>/Dev` that are real git repos, plus
   a few personal projects, `jarvis` and `brain` with
   `counts_only = true` for `brain` and `jarvis`), `sensitive_terms` for personal matters,
   extra sensitive globs for any folder that must never be read, the
   `work_metadata_to_claude` decision (design question 1), and `[hygiene].deny_substrings`
   or `state/hygiene-denylist.txt` for names that must never appear in the public repo.
2. `deploy\setup-venv.ps1`, then `python -m pytest -q` offline: green.
3. `jarvis self-test`: all PASS. `pytest -m live`: green (about $0.001).
4. `jarvis run-digest --dry-run`: the owner reads the exact payload and the held list and
   adds terms or globs until nothing that should stay on the machine is in the payload.
5. `jarvis run-digest --claude`: the note exists at
   `C:/Users/<owner>/brain/raw/jarvis/digest-<date>.md`, `jarvis audit verify` passes,
   cost under 0.60 USD, the owner judges the note useful. Confirm the toast appeared.
6. Record the real `StartBoundary` of your nightly notes job (if its display and its
   configured time differ) and note that reconcile's freshness check covers both.
7. `jarvis install-task --apply` (or `deploy\register-jarvisd-task.ps1`); `Get-ScheduledTask
   JarvisDaemon` shows AtLogOn and Daily 06:00 triggers, Interactive principal, Limited run
   level; `jarvis status` shows a fresh heartbeat and `mode: task`.
8. Kill-switch contract: run `bin\kill-switch.ps1 -Scope Daemon` in its documented dry-run
   mode (check its param block first) and confirm it reports the `JarvisDaemon` task as
   found; then, in a quiet moment, trip it for real, confirm the pythonw process and any
   `claude` child are gone and the task is Disabled, then re-enable the task and confirm
   the daemon comes back on the next trigger or a manual start. Record the result in the
   README v1 section.
9. The next morning: read the scheduled digest, run `jarvis wrong <id>` on at least one
   item to exercise the correction path, and record the day's cost from `jarvis audit cost`.

Acceptance: steps 1 to 8 logged with results; the scheduled digest for the following day
exists in the vault with `origin: schedule` or `catchup`; `jarvis audit verify` passes;
no file was created under `brain/telos`, `brain/notes` or `Documents/<work>` (directory
snapshot before and after, taken by the owner); the kill-switch contract step shows the
task stop ends the process.

Depends on: T11.

---

## T13. Stretch: ClickUp section through a scoped connector call

Files: `jarvisd/collectors/clickup.py`, `jarvisd/claude.py` (add profile `clickup_read`
and `complete_static`), `tests/test_collectors_clickup.py`,
`tests/fixtures/clickup_result_ok.json`, `tests/live/test_clickup_live.py`

Test to write first: `tests/test_collectors_clickup.py::test_api_token_never_read_or_logged`
(a fixture `clickup-config.json` with a planted canary `api_token`; the canary appears in no
variable passed on, no prompt, no audit record, no argv, no stdin).

Work:
- Behind `[digest].clickup_enabled` (default false). `ClaudeClient.complete_static(template,
  params, purpose)` takes a constant template and validated params only (each matching
  `^[0-9a-zA-Z:+._-]{1,40}$`), raises `TypeError` if given item data, and uses the
  `clickup_read` profile: `--model haiku`, `--setting-sources ""`,
  `--disable-slash-commands`, `--no-session-persistence`, `--permission-prompts none`,
  `--max-budget-usd 0.40`, `--allowedTools
  mcp__claude_ai_ClickUp__clickup_filter_tasks,mcp__claude_ai_ClickUp__clickup_get_task`,
  `--disallowedTools` naming every ClickUp write tool and every built-in tool, and
  `--output-format stream-json --verbose` so `tool_use` events are recorded. The exact
  treatment of `--tools ""` and `ToolSearch` is decided by the live check below and written
  into the profile with a comment citing the measured result.
- The prompt asks for tasks assigned to `developer.clickup_user_id` (read from
  `clickup-config.json` by explicit key; `workspace_id` likewise; `api_token` never
  referenced) with status not SHIPPED and due within 48 h or updated since the window
  start, plus the current task if open, as a JSON array of at most 30 objects
  `{task_id, name, status, due, updated, list, url}`.
- Output validated into `ClickUpTask` models; converted to deterministic-render Items that
  re-enter the tier gate (task names are untrusted text and pass `text_hit`); the result is
  rejected and the source marked partial if any `tool_use` outside the allowlist appears or
  `permission_denials` is non-empty (audited as a possible injection signal). Failure of any
  kind renders "ClickUp: unavailable (reason)" and never fails the digest.

Acceptance:
- Offline: canary token test passes; success fixture yields items; malformed, empty,
  over-30, forbidden tool, denial and timeout fixtures map to unavailable or partial with a
  reason string; audit records the call as `purpose = clickup_read`; the digest renders the
  ClickUp section deterministically.
- Live (`-m live`, about $0.19, second marker `clickup`): first run the measured shape
  (`--allowedTools` without `--tools ""`) and then the same with `--tools ""`; record which
  one returns a parseable JSON array with `permission_denials` empty, whether `ToolSearch`
  had to be allowed, the token count and the cost; freeze that argv in the profile and in
  the golden test. If neither works headless, keep `clickup_enabled = false` and leave the
  "ClickUp: disabled" line.
- No digest regression: the whole offline suite stays green with `clickup_enabled` both
  false and true (fake client).

Depends on: T12.
