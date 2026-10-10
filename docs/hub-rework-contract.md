# Hub rework contract (phases 1 and 2)

Status: the shared contract between the digest writer (`jarvisd/render.py`, `jarvisd/collectors/brain.py`,
`jarvisd/collectors/git.py`, `jarvisd/collectors/system.py`, `jarvisd/claude.py`) and the hub
(`jarvisd/hub/data.py`, `jarvisd/hub/views.py`, `jarvisd/hub/app.py`, `jarvisd/hub/check.py`). Two builders
implement it in parallel; the regexes here are the only agreement they need. Background: the audit synthesis
of 2026-10-09 and [hub](hub.md). Every name below is synthetic.

Owner decisions, not reopened: vault decisions are one line each, no rationale, window only, collapsed by the
hub; Done and Snooze are phase 3, so every item line carries a stable key; the avatar sits inside a one-line
status strip at every width, and on phones the strip replaces the pill, never the health word; item ids end
each item line as ` [xxxxxxxx]` and the hub hides them; no Claude one-liner is glued to a line.

## 1. The digest note after phase 1

### 1.1 Shared constants

`jarvisd/render.py` exports these; the hub imports them, so a rename fails at import, not in a browser.

```
GRAMMAR = 2                                  # front matter key `grammar`; absent means 1 (today's notes)
ID_TAIL = re.compile(r" \[(?P<id>[0-9a-f]{8})\]$")
HEADINGS = {"start_here": "Start here", "attention": "Attention", "active_task": "Active task",
            "still_open": "Still open", "decided": "Decided yesterday", "repos": "Repos",
            "system": "System", "held": "Held back and not summarized",
            "source_status": "Source status", "flag_mistake": "Flag a mistake"}
```

"Open threads" is a reserved heading (`FORBIDDEN_HEADING`, brain-nightly reads it as a session section), so
the open threads section is `## Still open`. `## Held back and not summarized` keeps its name because
`HELD_HEADING` in `jarvisd/hub/inbox.py` finds held evidence by it.

Id tails: Start here items, Active task, Still open, Decided yesterday, per-repo Repos lines, Attention lines
derived from an item. None on status sentences, System, Held, Source status, Flag a mistake. One tail per
line, nothing after it. `clean()` still applies to every string.

Normalised key, one implementation in `jarvisd/common.py`, used by the collector now and the hub in phase 3:

```
def norm_key(text: str) -> str:
    t = text.casefold()
    t = re.sub(r"\[\[[^\]]*\]\]", " ", t)                       # wikilinks
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", t)   # dates
    t = re.sub(r"\[[0-9a-f]{8}\]|\bw-[0-9a-f]{6}\b", " ", t)   # item and held ids
    t = re.sub(r"\((?:stale|see entry point)\)", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)                              # punctuation, backticks, brackets
    return " ".join(t.split())
```

### 1.2 Sections, in order

`DEFAULT_SECTIONS = ("start_here", "attention", "active_task", "still_open", "decided", "repos", "system",
"held", "source_status", "flag_mistake")`. A section whose body is empty is omitted with its heading
(Still open and Decided yesterday are the ones that can vanish); the hub treats a missing section as zero.

**Start here.** Line 1 is the headline (Claude's, or the fallback sentence), no bullet. Then at most 5:

```
^(?P<n>[1-5])\. (?P<text>[A-Z][^\[]{9,158}) \[(?P<id>[0-9a-f]{8})\]$
```

Text is verb-first and names a deadline or a consequence. `SYSTEM_PROMPT` in `jarvisd/claude.py` gains
"each why starts with an imperative verb and states the deadline or what happens otherwise; skip items that
read as done, delivered, shipped, cosmetic or optional, or already covered by another", and tells the model
what the page already shows (active task, broken CI, failed jobs, the open threads), so the headline names
only what the list does not. The prompt no longer asks for per-item `summaries` (nothing consumes them; the
field stays tolerated on `DigestSummary`). Only `cleared_ids` are accepted, and a why is dropped unless
`render.imperative` holds: its first word is not a determiner, pronoun or time word (`NOT_IMPERATIVE`) and
no auxiliary verb follows it ("Rotate the key before the demo", not "The key was pasted"). The fallback
then fills the list. `_fallback_attention` is deterministic: score 4 for an overdue or due-today task, 3
for a thread whose `norm_key` contains a word of
`DEADLINE_WORDS = ("deadline", "due", "overdue", "today", "tomorrow", "blocked", "blocker", "blocks",
"waiting on", "expires", "rotate", "urgent", "avant", "bloque", "echeance")` ("before" and "by" are not
deadline words: "before any client use" and "by hand" are not deadlines) and that the Still open screen below
does not exclude, 3 for CI failing, 2 for a PR awaiting review, 1 for a repo with commits, 0 otherwise; sort
by (score desc, date desc, id asc); the fallback template always starts with a verb and carries a consequence
("Finish <task>, overdue since 2026-10-05", "Act on this thread, it names a deadline or a blocker: <text>",
"Fix example-api: CI is failing on main, merges are blocked"). Example:

```
## Start here
One task is overdue and one repo moved overnight.
1. Rotate the pasted sandbox key before the demo tomorrow [c0ffee01]
2. Answer the reviewer on the parser fix, blocked since Monday [e5f6a7b8]
```

**Attention.** Deterministic, no Claude. Either the single line `- Nothing broken.` or one line per anomaly,
in this fixed kind order:

```
^- (?P<kind>CI failing|Job failed|Daemon crashed|Unclean exit|Task overdue|Backlog held|Breaker open|Config invalid): (?P<text>[^\[]+?)(?: \[(?P<id>[0-9a-f]{8})\])?$
```

Sources: `github_repo` items with `ci` in `{failure, timed_out, startup_failure, action_required}` (id = the
repo item); system facts `jobs_failed`, `daemon_crashes` (new fact, `count("daemon_crash")`),
`unclean_exits`, `config_invalid` (new fact, `count("config_invalid")`); `active_task` with `due_state ==
"overdue"` (id = the task item); `queue.held >= HELD_BACKLOG_ALERT` (render constant, 20);
`breaker_state != "closed"`. Example: `- CI failing: example-api on main, 2 runs in a row [a1b2c3d4]`.

**Active task.** One line, the plumbing suffix "No ClickUp call was made (v1)" is gone, an id tail is added:

```
^- (?P<task_id>\S+) (?P<title>.+?), status (?P<status>[^,]+), (?P<due>due \d{4}-\d{2}-\d{2}(?: \((?:OVERDUE|TODAY)\))?|no due date)\. \[(?P<id>[0-9a-f]{8})\]$
```

The three fixed sentences (no task, withheld, unavailable) stay as they are, without a tail.

**Still open.** Built in `BrainCollector.collect` after `_bullet_items` and `_session_items`: every
`brain_thread` and `brain_session` item gets `meta["key"] = norm_key(text)`; duplicates on the key collapse,
the RECENT.md bullet wins (it has a date), and the dropped session item contributes `meta["session"]` to
the survivor for grouping. Excluded before the cap (regex on `norm_key`, so case and punctuation do not
matter): `age_days > STALE_AFTER_DAYS`; ids already printed in Start here;
`RESOLVED = r"^(done|delivered|shipped|resolved|merged|closed)\b|\b(is|was|now) (done|delivered|shipped|resolved|merged|closed)\b|\b(done|delivered|shipped|resolved)$"`;
`COSMETIC = r"\b(cosmetic|optional|nice to have|low priority)\b"`;
`NO_ACTIVE_WORK = r"^no active work\b"`; wikilink-only lines, where the key after removal is empty or
matches `^(see|voir|cf|and|et|the|related|ref|refs|aussi|also)( |$)*$`; reference lines, whose text starts
with `REFERENCE_PREFIX = r"^\s*(?:[-*]\s+)?(?:related|see also|voir aussi|refs?|references?)\s*:"` whatever
follows the colon (`common.noise_line` is the one implementation of these three, used by the collector, the
writer and the hub's grammar 1 fallback); threads that quote the active task's tracker id (the Active task
line already prints it). One language per line: the item text is printed as written
and no one-liner is appended. Grouped by `group` = the session slug without its `YYYY-MM-DD-HH-` prefix,
hyphens to spaces, else `notes`; groups ordered by their newest item, lines within a group by date desc
then id. Cap 10 visible, then one count line.

```
^- (?P<group>[^:\[\]]{1,60}): (?P<text>[^\[]+?)(?: \((?P<age>\d+)d\))? \[(?P<id>[0-9a-f]{8})\]$
^- (?P<hidden>\d+) more open threads? not shown \(cap 10\)\.$
```

`(Nd)` appears when `age_days >= 3`. Example:
`- parser rework: Confirm the retry budget with the reviewer (4d) [6b6b6b6b]`.

**Decided yesterday.** `brain_decision` items whose `meta.date` lies in `[window_start.date(),
window_end.date()]`, newest first, cap 10, the rationale cut at the first match of
`RATIONALE = r"\s*[,;:]?\s*\b(because|parce que|car|rationale)\b.*$"`, and before that at the first spaced
dash (`DASH_RATIONALE`: an em or en dash followed by a space, or a spaced hyphen), because `clean()` would
turn that dash into a comma and hide the cut point:

```
^- (?P<text>[^\[]{3,200}) \[(?P<id>[0-9a-f]{8})\]$
```

Example: `- Keep the strict sum of 100 for the scoring quotas [9f8e7d6c]`.

**Repos.** A repo line is printed only when `commits > 0`, `ahead > 0`, `behind > 0` or
`commits_withheld > 0`; the grammar of `_REPO_LINE` is unchanged except that nothing follows the id:

```
^- (?P<name>\S+?)(?: \((?P<tag>work)\))?:? (?:branch (?P<branch>.+?), )?(?P<commits>\d+) commits? since window, (?P<modified>\d+) modified, (?P<untracked>\d+) untracked(?P<rest>[^\[]*) \[(?P<id>[0-9a-f]{8})\]$
```

Repos with uncommitted files and no commits keep their counts on one line, so the Projects dirty rule
still has data: `^- Uncommitted only: (?P<list>(?:\S+ \d+/\d+)(?:, \S+ \d+/\d+)*)\.$`, each entry
`name modified/untracked`. Then `^- Quiet: (?P<n>\d+) repos?\.$` (count only; a configured repo named
nowhere in a grammar 2 Repos section is quiet with zero counts). "Not a git repo or missing" and "Could not
be read" lines stay as they are. GitHub: `_github_line` is printed only for repos with open PRs or failing
CI, tail unchanged (`[id]` last); the rest fold into `^- GitHub quiet: (?P<n>\d+) repos?, CI green or
none\.$` and `^- GitHub not read: (?P<n>\d+) repos? \((?P<states>[a-z_]+ \d+(?:, [a-z_]+ \d+)*)\)\.$`, for
example `- GitHub not read: 16 repos (no_access 15, no_remote 1).` The three fixed GitHub sentences stay.

**System.** Heading `## System`. When nothing is abnormal, exactly one line:
`^- All green: (?P<jobs>\d+) jobs? done, 0 failed, \$(?P<usd>\d+\.\d{2}) Claude, breaker closed, disk ok, tasks ok\.$`.
Otherwise one line per anomaly, `^- (?P<what>[A-Z][^:]{2,40}): (?P<detail>.+)\.$`, in this order: Jobs
failed, Daemon crashed, Unclean exits, Breaker, Kill switch, Watchdog, Disk, Task (one per task with a bad
result), Config invalid, Notes index stale (RECENT.md over 30 h), Sessions not filed (orphan checkpoints
>= 5), Held backlog (>= 20). `collectors/system.py` decodes `LastTaskResult` through
`TASK_RESULTS = {0: "ok", 1: "script error", 267009: "still running", 267011: "never ran",
267014: "stopped by the user", 2147750687: "an instance was already running",
2147943623: "cancelled", 2147946720: "refused by the operator or administrator"}`; an unknown code prints
`0x%08X (unknown)`. A task line is an anomaly unless the code is 0 or 267009. Example:
`- Task: ExampleNightly last ran 2026-10-09 06:00, refused by the operator or administrator (0x800710E0).`
Two result lines, not anomalies, follow the verdict: consolidation when it ran, and
`- Checkpoints waiting for /promote-sessions: N.` when `0 < N < 5` (at 5 it becomes the "Sessions not filed"
anomaly above). The hub's Today view shows the grammar 1 System block through the label map too: a raw
`result <code>` is decoded and `RECENT.md age N h` reads "notes index updated N h ago".

**Held back and not summarized.** One line:

```
^- Held: (?P<sens>\d+) sensitive(?: \(ids (?P<ids>[^;]+); reasons: (?P<reasons>[^)]+)\))?, (?P<pol>\d+) policy, (?P<cap>\d+) over cap\. Claude: (?P<claude>[a-z_ ]+)\. Run `jarvis held` in a terminal\.$
```

Ids keep the `MAX_HELD_IDS` rule; the Inbox still finds a held evidence id by word boundary in this line.
Source status and Flag a mistake are unchanged.

### 1.3 Front matter

Existing keys stay (goldens, brain-nightly, ask and propose read them). New scalar keys, so the hub reads
them from `split_front_matter` without parsing the `items` dict: `grammar: 2`, `n_collected`, `n_cleared`,
`n_held` (sensitive plus policy), `n_start_here`, `n_attention` (0 when "Nothing broken."), `n_still_open`,
`n_still_open_hidden`, `n_decided`, `n_repos_active`, `n_repos_quiet`, `n_system_anomalies`. The hub shows
"87 collected, 76 summarised, 11 held" from the first three.

## 2. The hub after phase 2

### 2.1 Views, routes, redirects

`NAV = (("/", "Today"), ("/inbox", "Inbox"), ("/projects", "Projects"), ("/activity", "Activity"))`, same
tuple in `check.VIEWS` plus `("Face", "/face")`. `/face`, `/api/status`, `/digest/{job_id}` and the static
routes stay. Old routes answer 301 so bookmarks survive: `/runs` to `/activity#runs`, `/ledger` to
`/activity#delivered`, `/held` to `/activity#held`, `/audit` to `/activity#audit`, `/status` to
`/activity#status`, `/repos` to `/projects`, `/reminders` to `/inbox?sort=due`. Query strings are dropped.

### 2.2 Status strip

Rendered by the server in `views.page` from `data.strip()` and refreshed by the face poll (which already
fetches `/api/status` every 5 s). Fields used: `running`, `kill`, `pause`, `breaker.state`,
`last_digest.job_id`, `last_digest.status`, `last_digest.at` (new, the finish stamp), `next_due`,
`face.inbox_pending`, `queue.failed`. Text, one line, in this order: health word `Running.` | `Stopped.` |
`Paused.` | `Kill switch on.` | `Claude calls paused.` (breaker open); `Last digest HH:MM.` or
`Last digest HH:MM, failed.` or `No digest yet.`; `Next HH:MM today|tomorrow.`; `N waiting.`;
`N failed.` The avatar thumbnail (1.75rem under 40rem, 2.75rem above, the 88rem rail unchanged) sits at the
strip's right end and links to `/face`. Under 40rem the pill is hidden and the strip's health word is the
health signal; the pill keeps its `aria-live` text for assistive tech.

### 2.3 Today

Parsing lives in a new pure module, jarvisd/hub/digestparse.py, importing `HEADINGS`, `ID_TAIL` and
`GRAMMAR` from `jarvisd/render.py`, with the grammar 1 regexes as fallback when `grammar` is absent. Its
tests feed real `render_digest` output from the `tests/test_render.py` fixtures. Blocks, each an `h2`:

1. **Attention**: the Attention lines as rows with the `bad` pill, or "Nothing broken."
2. **Needs you**: the headline as lede, then `<ol class="needs">` of Start here lines,
   `<li data-id="c0ffee01">text</li>`; ids are not visible.
3. **Waiting for you**: "N waiting" linking to `/inbox`, then the three oldest `proposed` titles, each with
   an inline Confirm form (`POST /inbox/{id}/confirm`, same handler, Host guard, Origin check and CSRF token;
   a hidden `next` field accepted only as the literal `/` or `/inbox`, anything else is 400). Reject and
   Edit stay on the Inbox.
4. **Changed since yesterday**: the repo delta between the latest note and the newest note of an earlier
   date: `name: N commits`, `name: uncommitted M/U, was M/U`, `name: CI now failing|green`, `name: N open
   PRs, was M`, then `N decisions recorded`, `held N, was M`. Empty means "Nothing changed."
5. `<details id="else">` **Everything else**: Still open grouped by its `group`, Decided yesterday, System.
6. `<details id="full">` **Full digest**: the current `digest_article` (metadata card and markdown).
7. `<details id="ids">` **Item ids**: `jarvis wrong <id>` per Needs you line.
8. `<p class="end">That's all.</p>`

The hub strips `ID_TAIL` from every visible line and keeps the id in `data-id`. `<main id="main">` and the
word "read-only" stay; the face companion stays outside `<main>`.

### 2.4 Inbox, Projects, Activity

Inbox card, in order: title, why (rationale clipped to 160), pills (project, `due in N days` /
`N days late` / `due today`, `outcome unknown` when the attempt marker exists), the three buttons; evidence
rows and ids in `<details>`; evidence ids missing from the run's note collapse to one line. `?sort=due`
orders by due date, undated last; default is newest first. Every mechanic in [hub](hub.md) "The write
actions" is unchanged.

Projects: **Active** repos first (commits in the latest note, open PRs, failing CI, any risk, or
uncommitted work unless exempt), as rows with branch, delta since yesterday, risk pills and the active
task; then one line "Quiet: N repos" with a `<details>` naming them. New key `[hub].always_dirty`, a list of
repo names (set in `jarvis.local.toml`, the tracked default is empty): those repos never get the
"uncommitted work" risk and dirty counts alone do not make them Active. The old Repos table and its
"other lines" live in `<details id="raw-repos">` on this page.

Activity opens on a 7-day strip of tiles computed from the cached audit snapshot and the manifests: runs,
failed, cost USD, proposals made / confirmed / rejected, held, flagged wrong (`correction` events), chain
verified, next digest due. Below, `<details>` with ids `runs`, `delivered` (the ledger), `held`, `audit`
(the chain card stays open above its table) and `status` (the CLI lines). Collapsing uses native
`<details>`; open state is saved per `id` and path in `localStorage` by a new file served at
`/static/prefs.js` (loaded on every page, also at `refresh_s = 0`), and restored after `hub.js` swaps
`<main>`, which now dispatches a `hub:refreshed` event on `document`. No inline script, no `style=`, no
`http` string, CSS stays between 100 and 400 lines.

## 3. Label map

| Jargon | Shown as |
|---|---|
| audit seq, witness | Audit record N |
| local_tier not_installed / unavailable / up | Local model: not installed / unavailable / up |
| items {collected..} | 87 collected, 76 summarised, 11 held |
| held_policy, held_sensitive | kept back (work metadata), kept back (sensitive, never read) |
| term:3, tag_frontmatter | matched private rule 3, tagged sensitive |
| over_cap | too long to send |
| result 2147946720 | refused by the operator or administrator (0x800710E0) |
| degraded_no_llm, partial, complete, failed, noop | written without Claude, written with a source missing, written, failed, nothing to do |
| breaker open / closed | Claude calls paused / Claude calls allowed |
| watermark | digest window start |
| heartbeat N s ago | last sign of life N s ago |
| kill file present | stopped by the kill switch |
| proposed, confirmed, edited_confirmed, rejected | waiting for you, confirmed, confirmed with edits, rejected |
| Runs, Ledger, Reminders, Status | Digest runs, Delivered, due pills, Daemon status |
| consolidation candidates | memory candidates |
| orphan checkpoints | sessions not yet filed |
| RECENT.md age | notes index updated N h ago |
| counts_only | counts only, names kept back |
| stale (thread), days idle 14+ | N days old, no activity in at least N days |
| tier violation | a private term reached the Claude step, call aborted |
| Source status | what was read |

## 4. Performance

- `/api/status` never parses the whole done queue. `_last_digest` reads the three newest files of
  `queue/done` by mtime (the `face_signal` rule) and `held_count` is cached by a directory signature
  (name, size, mtime_ns per file), the way `audit()` caches. Test: on a warm cache a second `status()` call
  reads at most 10 files (count `_read_json` calls).
- Runs is linear: `AuditSnapshot` gains a `seq` to hash index built once; `witness` is a lookup. `runs()`
  and `ledger()` share `_manifests()` and each queue state is parsed once per request.
- One model per request: each route calls one `HubData` method that returns everything its view needs
  (`today()`, `projects_page()`, `activity()`); views are pure and never call back into data.
  `_digest_history` is cached by the signature of the digest folder.
- The face poll stays at 5 s because `/api/status` is now cheap; an ETag on the refresh is optional.

## 5. Tests

Must change: `tests/test_hub.py` (`VIEWS`, the pinned labels in `test_every_view_renders` and
`test_hub_check_passes_on_a_seeded_tree` become Today, Inbox, Projects, Activity; the fixture note is
rewritten in grammar 2 with synthetic names; the Repos and Status checks move to Projects and Activity),
`jarvisd/hub/check.py` `VIEWS`, `tests/test_hub_projects.py` (`/ledger` and its `href` become
`/activity`), `tests/test_hub_reminders.py` (the view checks go through `/inbox?sort=due`, the bucket tests
stay on `data.reminders()`), `tests/test_render.py` (the four goldens are regenerated once, deliberately;
the front matter key test gains the new keys; `test_default_section_order_covers_the_design`), new tests in
`tests/test_collectors_brain.py` (key, de-dup, exclusions), `tests/test_collectors_system.py` (decode
table, new facts), `tests/test_inbox.py` (the one-line Held section, the slim card, the `next` field),
`tests/test_hub_face.py` (prefs.js present, strip markup), the views table in `docs/hub.md`.

Must not change: the CSP and `test_security_headers`, `test_the_hub_never_writes`,
`tests/test_write_locations.py`, the hub import ban and banned method names, the refresh rule (`hub.js`
absent at `refresh_s = 0`), the CSS line bounds and `fetch(` in `hub.js`, `<main id="main">` and
"read-only" on every view, `tests/test_digest_e2e.py`, `tests/test_repo_hygiene.py`, `tests/test_docs.py`.

## 6. Item sidecar and history (writer side, phase 3)

`jarvisd/digest.py` writes both files through `atomic_write_text` after `write_raw` succeeded, from the same
`View` the sections used: a sensitive-held id is not in `View.visible`, so it cannot reach either file. The hub
reads both and writes neither.

**Sidecar** `state/runs/<job>/items.json`: `{"job_id", "date", "items": [...]}`, one record per rendered item
line, in page order:

```
{"key": "rotate the pasted sandbox key before the demo tomorrow", "id": "c0ffee01", "section": "start_here",
 "group": "notes", "date": "2026-10-08", "text": "Rotate the pasted sandbox key before the demo tomorrow",
 "rank": 1, "since": "new"}
```

`key` = `item.meta["key"]` if set, else `norm_key(item.text)`; `section` in start_here | attention | active_task
| still_open | decided | repos; `group` as printed in Still open, else `notes`; `date` = `item_day`; `text` = the
rendered line without `ID_TAIL`, the `group: ` prefix and ` (Nd)`; `rank` from 1 within the section; `since` in
new | returned | "" (6.2). An id printed twice is recorded once, Start here first.

**History** `state/item-history.json`: `{"updated": "<run date>", "last_run": "<job id>", "items": {key:
record}}`, read-modify-write under `path_lock` plus `FileLock(lock_path_for(path))`, writer only:

```
{"id": "c0ffee01", "text": "Rotate the pasted sandbox key ...", "first_seen": "2026-10-07",
 "last_seen": "2026-10-09", "times_shown": 3, "sections": ["start_here", "start_here", "still_open"],
 "status": "open", "snoozed_until": null, "resolved_at": null}
```

`sections` logs one entry per run that showed the key, so `sections.count("start_here")` is the Start here budget
and `len(sections) == times_shown`; `id` and `text` are the last rendered values; `status` in open | done |
snoozed | dropped.

### 6.1 Aging, applied while building the note

Per run: load history and `state/attention/*.json` (section 7), build the plans, then update history from the
sidecar.

1. Done: a key with `status == "done"` is excluded from every section and the fallback, for ever.
2. Snoozed: an attention file with `action == "snooze"` and `until > today` sets `status = "snoozed"` and
   `snoozed_until`; the key is excluded from Start here, Still open and the fallback. On `today >= until` it is
   open again, printed with `since: "returned"`. A changed text is a new key, hence a new open item at once: the
   Linear Triage rule falls out of the key.
3. Start here budget: `render.pick_start_here` drops every id whose key has `sections.count("start_here") >= 2`
   before Claude's picks and the fallback apply; the item then follows the Still open rules, `(Nd)` unchanged.
4. Decisions: a `brain_decision` key is printed once; at first print it is filed `status = "done"`, `resolved_at =
   run date`, so a rerun cannot repeat it.
5. Dropped: after the sidecar is applied, every open key with `last_seen` older than 7 days becomes `status =
   "dropped"`, `resolved_at = today`: gone from the daily note, listed in the weekly review. Collected again, it
   reopens with its old `first_seen`.

### 6.2 Since yesterday

Front matter, not a new section: the hub already reads scalars from `split_front_matter`, brain-nightly ignores
unknown keys, and no text is repeated where the filter could be missed. New keys:
`n_since_new` (keys first seen this run), `n_since_resolved` (done this run, by click or decision),
`n_since_dropped`, `n_since_returned`. The detail is `since` on sidecar records and `resolved_at == run date` in
history. The hub's "Changed since yesterday" block gains, after the repo lines, `N new threads`, `N threads
resolved`, `N dropped`, `N back from snooze`, each followed by its texts from history, at most 5.

## 7. Done and Snooze (hub side)

Routes `POST /today/{item_id}/done` and `POST /today/{item_id}/snooze`, one handler `decide_item` in
`jarvisd/hub/app.py` calling the new module jarvisd/attention.py (beside `jarvisd/inbox.py`, not under hub/),
shared with the CLI twin `jarvis attend <id> --done | --until <date>`. The Inbox guards in the Inbox order (Host,
Origin, content type, size and field caps, CSRF in constant time), then:

- `item_id` matches `^[0-9a-f]{8}$` and is in the latest run's sidecar, else 404; the key comes from that record,
  never from the form. Without a sidecar (older notes) the hub shows no buttons.
- `until`: tomorrow | 3d | monday (next Monday, never today) | `YYYY-MM-DD` after today, within 90 days; else 422.
- One lock, the Inbox `_Locked` pattern on `state/attention/` (`path_lock` plus `FileLock`, 20 s); not acquired is
  `busy`, 409.
- One file per key, `state/attention/<name>.json`, `name = "-".join(key.split())[:72] + "-" + sha256_hex(key)[:8]`
  (helper `attention_name` in `jarvisd/common.py`): `{"key", "id", "action": "done" | "snooze", "until":
  "YYYY-MM-DD" | null, "decided_at": iso, "note": "<digest job id>"}`, via `atomic_write_text`.
- A decision in force (a done, or a snooze with `until > today`) is 409 `decided`, nothing changes; an expired
  snooze is replaced.
- Audit `attention_decided` (`item_id`, `action`, `until`, `digest_run_id`); a save failure emits
  `attention_decide_failed` (`error`), 502. No text and no key is audited.
- Success is 303 to `/?ok=done&id=<id>` or `/?ok=snoozed&id=<id>`; flash "Done." or "Snoozed until <date>.".
  `data.today()` reads `state/attention/` and hides lines with a decision in force (one line `N decided, applied
  at the next digest` in Everything else), so the line is gone at once.

Markup, Needs you and Still open alike (`[csrf]` is the hidden token field):

```
<li data-key="rotate the pasted sandbox key before the demo tomorrow" data-id="c0ffee01">
 <span class="t">Rotate the pasted sandbox key before the demo tomorrow</span>
 <span class="act"><form method="post" action="/today/c0ffee01/done">[csrf]<button>Done</button></form>
 <details class="snooze"><summary>Snooze</summary><form method="post" action="/today/c0ffee01/snooze">[csrf]
  <button name="until" value="tomorrow|3d|monday">...</button> x3 <input type="date" name="until">
  <button>Pick</button></form></details></span></li>
```

CSS: `li[data-key] {display:flex; flex-wrap:wrap; gap:.5rem}`, `.t {flex:1 1 20rem; min-width:0}`,
`.act {margin-left:auto; white-space:nowrap}`: one line at 1440; at 375 `.t` takes the full width and `.act`
wraps under it. `.snooze` has no `id`, prefs.js ignores it. Buttons are 2rem tall.

## 8. Seen marks (hub side)

`/static/prefs.js` gains a second block, no network, no cookies. `<section id="today"
data-digest="digest-2026-10-09">`, the first element in main, carries the digest id (`<main id="main">` is pinned
byte for byte).
On load and on `hub:refreshed`: read `data-digest` and every `[data-key]`; load `hub:seen` = `{"digest": D,
"keys": [...], "prev": [...]}`. If the page's digest id differs from `D`: `prev = keys`, `keys` = the page's keys,
`digest` = the page's id, save; otherwise store nothing. Then add class `seen` to every line whose key is in
`prev`. A reload or a 30 s refresh of the same digest stores nothing; a thread carried over from the last digest
you saw is dimmed (`.seen .t {opacity:.6}`, still readable). No storage, nothing dimmed.

## 9. Weekly review (both sides)

Writer: after a successful note write, on the first run of a new ISO week, when `weekly-<week just ended>.md` is
absent, `jarvisd/digest.py` renders `render.render_weekly` and calls `deps.vault.write_raw("weekly-YYYY-Www.md",
text, job_id)`; `jarvis weekly [--week YYYY-Www] [--dry-run]` prints or writes the same way (the marker rule lets
it rewrite its own file). Sources: history, attention files, `state/runs/*/run.json`, audit `correction` events,
all filtered when first persisted. Front matter: `type: jarvis-weekly`, `week`, `from`, `to`, `generated`,
`cost_usd`, `n_runs`, `n_failed`, `n_decided`, `n_dropped`, `n_done`, `n_snoozed`, `n_flagged`. Title
`# Week 2026-W41`, then six `##` sections in this order, an empty one printing `- None.`, newest first, cap 30:

```
Runs              ^- (?P<runs>\d+) runs?, (?P<failed>\d+) failed, \$(?P<usd>\d+\.\d{2}) Claude\.$
Decided this week ^- (?P<date>\d{4}-\d{2}-\d{2}): (?P<text>[^\[]{3,200}) \[(?P<id>[0-9a-f]{8})\]$
Dropped threads   ^- (?P<text>[^\[]{3,200}) \((?P<first>\d{4}-\d{2}-\d{2}) to (?P<last>\d{4}-\d{2}-\d{2})\) \[(?P<id>[0-9a-f]{8})\]$
Snoozed and done  ^- (?P<action>Done|Snoozed until \d{4}-\d{2}-\d{2}) (?P<date>\d{4}-\d{2}-\d{2}): (?P<text>[^\[]{3,200}) \[(?P<id>[0-9a-f]{8})\]$
Flagged wrong     ^- (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}): (?P<id>[0-9a-f]{8}|w-[0-9a-f]{6}), should (?P<should>escalate|hold|skip|other)(?P<leak>, leak)?\.$
Cost by day       ^- (?P<date>\d{4}-\d{2}-\d{2}): (?P<runs>\d+) runs?, \$(?P<usd>\d+\.\d{2})\.$
```

Decided and Dropped come from history (`resolved_at` in the week), Snoozed and done from attention files
(`decided_at`), Flagged wrong from the audit (ids only), Cost by day from the manifests.

Hub: `HubData.activity()` adds `week` (the newest `weekly-*.md` in the raw folder, parsed by
`digestparse.parse_weekly`, same take-by-heading, regex-per-line, raw-fallback pattern) and `flagged` (the
`correction` events of the last 7 days as `{ts, item_id, should, leak}`). Activity renders `<details id="week">`
"This week" after the strip: the six sections, then `<h3>Flagged wrong</h3>` from `flagged`, shown even without a
note ("No weekly note yet.").

## 10. Tests for phases 3 and 4

| Must change | Why |
|---|---|
| `tests/test_hub.py` Today and Activity tests | data-key, buttons, `#today[data-digest]`, hidden decided lines, "This week" with and without a note |
| `tests/test_render.py` goldens, front matter keys, weekly tests | `n_since_*` (regenerated once); the weekly grammar, `- None.`, `parse_weekly` on real output |
| `tests/test_digest_e2e.py` | sidecar (no held id) and history after a run; neither on a dry run or a failed vault write |
| `tests/test_write_locations.py` | `WRITERS` unchanged (writes go through `jarvisd/fsio.py`); a new test pins the state paths jarvisd/attention.py and `jarvisd/digest.py` may name |
| `tests/test_hub_face.py` prefs test, `test_static_assets`, `docs/hub.md` | `hub:seen` in prefs.js; "The write actions" gains Done and Snooze |
| new tests/test_attention.py | mirrors `tests/test_inbox.py`: the guards, 404, 422, 409, lock, audit record, one file, redirect |

Must not change: `test_the_hub_never_writes` (GET only),
`test_mutating_methods_are_refused`, the CSP test, the import ban and banned method names (`decide_item` and
`attention_name` are not on it), `NAV`, `check.VIEWS`, the nav pins, the CSS bounds, `<main id="main">` and
"read-only", `tests/test_repo_hygiene.py`, `tests/test_docs.py`.
