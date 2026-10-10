# The work hub

What this is: a small web page served from your own machine that shows what the JARVIS
daemon has already recorded, and, in the Inbox, lets you confirm, edit or reject the task
proposals of [proposals](proposals.md). Every GET is a viewer: it reads the daemon's files and
never writes one, never takes a lock, never calls Claude and never starts a job. Two kinds of click
are the exceptions: the Inbox decisions, and Done or Snooze on a Today line. See "The write actions".

```
jarvis hub                 # serve on http://127.0.0.1:8765/ until Ctrl+C
jarvis hub --port 9000     # another port (1024 to 65535)
jarvis hub --check         # render every view once against the real state, then exit
```

## Status, stated plainly

- **Built and tested:** the four views (Today, Inbox, Projects, Activity) and the face, the Inbox with its three
  POST routes, Done and Snooze on Today with their two POST routes (`jarvisd/attention.py`), the seen marks and
  the "This week" disclosure, the loopback, Host and Origin guards, the CSRF token, the read-only data layer,
  the digest and weekly parsers (`jarvisd/hub/digestparse.py`) and `jarvis hub --check`. The suites are
  `tests/test_hub.py`, `tests/test_hub_digestparse.py`, `tests/test_hub_projects.py`,
  `tests/test_hub_reminders.py`, `tests/test_hub_face.py`, `tests/test_inbox.py` and
  `tests/test_attention.py`. They drive the real application through FastAPI's `TestClient` over a throwaway
  tree that the daemon's own writers filled, and `tests/test_hub.py` compares the whole tree byte for byte
  before and after every read-only view. The Inbox tests use a fake tracker, or the markdown adapter on a
  throwaway vault.
- **Never done for real:** a confirm or a reject outside the tests, a confirm against the live
  ClickUp service, a confirm of a proposal made by the real model (no paid proposals run has
  been made), and a Done or Snooze on a real digest line. Everything the Inbox does has met only a fake
  tracker, the markdown adapter on a throwaway vault and a local stand-in for ClickUp; Done and Snooze
  have met the test tree only.
- **Not built:** pushed reminders (ntfy), ClickUp due dates on the Inbox pills, local triage of events, Slack data, a SQLite index,
  authentication, TLS. The page has no login because it listens on loopback only.
- **Tested against fakes only:** the ClickUp adapter has never talked to the real ClickUp service,
  only to a local stand-in server. Use `dry_run` first (below).
- **Verified through the tailnet name from the same machine, not yet from a phone:** the
  `tailscale serve` steps below were run on 2026-10-09 (Tailscale 1.102) and `/face` answered 200
  at `https://<machine>.<tailnet>.ts.net/` with the name listed in `allowed_hosts`. A second
  device has not opened it yet.
- **Looked at in a browser once, at 1440 and 375 px, after the phase 2 rework (2026-10-09).** The tests prove
  the HTML, headers and data; the screenshots proved the layout once. Open it after any change to the CSS.

## The views

Four tabs and the face. The order on Today is fixed and ends with "That's all." so the page has an end; the
record-keeping views open on a summary and keep their tables behind disclosures. Section 2 of
`docs/hub-rework-contract.md` is the specification the code follows.

| View | What it shows | Where the data comes from |
|---|---|---|
| Today | Attention (what is broken, or "Nothing broken."), Needs you (the headline and at most five ranked lines, each with a Done button and a Snooze disclosure), Waiting for you (the Inbox count and the three oldest proposals with an inline Confirm), Changed since yesterday (the repo delta against the previous note, decisions recorded, held count, then the new, resolved, dropped and returned threads with their texts), then three disclosures: Everything else (active task, still open threads by group with the same two controls, decided yesterday, system; a line "N lines decided, applied at the next digest" when a decision hides something), Full digest (the metadata card and the whole note) and Item ids (the `jarvis wrong <id>` command per ranked line). A line carried over from the last digest you saw is dimmed (seen marks, below) | the two newest digest notes in the vault's `raw/jarvis` folder, parsed by `jarvisd/hub/digestparse.py`; the run's item sidecar `state/runs/<job>/items.json` and `state/item-history.json` (written by the digest); `state/attention/`; `state/proposals/` |
| Inbox | The proposals waiting for a decision as slim cards: title, why, pills (project, due, outcome unknown), the three buttons; the identifiers and the evidence behind "Evidence and ids". Newest first; `?sort=due` orders by due date, undated last. Evidence ids the run's note does not carry collapse to one line | `state/proposals/`, the digest note of the proposal's run, `queue/held/` |
| Projects | Active repositories first (commits in the latest note, open pull requests, failing CI, any risk, or uncommitted work unless the repo is listed in `[hub].always_dirty`): branch, what moved since yesterday, the counts, GitHub, risk pills (red for CI failing and an overdue task, amber for uncommitted work and a stale repo) and the active task, open proposals (a zero is an empty cell). Then "Quiet: N repos" with a disclosure naming them with their idle days, and the Repos table as collected behind "Repos as collected". A card list under 40rem; each Active row is anchored `#repo-<name>` so an Attention row on Today can point at it | the digest notes (up to 60), `state/proposals/`, the config |
| Activity | A strip of ten tiles for the last 7 days (digest runs, failed, Claude USD, proposals made, confirmed, rejected, kept back, flagged wrong, chain verified, next digest), the chain card and today's budget, a "This week" disclosure (the newest weekly review note, section by section, then "Flagged wrong": the `correction` audit events of the last 7 days as id and reason, shown even before the first weekly note), then disclosures: Digest runs, Delivered, Kept back, Audit records and Daemon status | `state/runs/<job>/run.json`, `queue/*/`, `logs/jarvisd-audit*.jsonl`, `state/budget.json`, `state/proposals/`, `queue/held/`, `state/`, the newest `weekly-YYYY-Www.md` in `raw/jarvis` |
| Face (`/face`, not in the nav) | The lantern avatar, with a state taken from `/api/status`; see "The face" | `/api/status`, the avatar folder |

`/api/status` returns the Daemon status data as JSON, plus `last_digest.at` (the finish stamp the strip
shows) and the `face` block. `/digest/<job id>` shows one digest note; the Digest runs table links to it.

### Old addresses

The views that the rework folded away answer `301 Moved Permanently` to the place that absorbed them,
query string dropped, so a bookmark or a `tailscale serve` link keeps working: `/runs` to
`/activity#runs`, `/ledger` to `/activity#delivered`, `/held` to `/activity#held`, `/audit` to
`/activity#audit`, `/status` to `/activity#status`, `/repos` to `/projects` and `/reminders` to
`/inbox?sort=due`. The table is `REDIRECTS` in `jarvisd/hub/views.py`. 301 rather than 308 because every
redirected route was GET only.

### The status strip

Every page opens with one line, first in the header at every width, rendered by the server from the same
data `/api/status` returns and repainted by the face poll every 5 seconds: the health word (`Running.`,
`Stopped.`, `Paused.`, `Kill switch on.` or `Claude calls paused.` when the breaker is open), `Last digest
HH:MM.` (or `, failed.`, or `No digest yet.`), `Next HH:MM today|tomorrow.`, `N waiting.` (proposals in the
Inbox) and `N failed.` (jobs in `queue/failed`). Under 40rem the strip shows a shorter line (the last digest
only when it failed or is missing, no `0 failed.`), so it stays one line on a phone. The avatar thumbnail
docks at its right end in a 2.75rem link at every width (the puppet inside it is 1.75rem under 40rem); on a
screen of 88rem and more the 10rem companion rail in the right margin stays. The health word in bold is the
one live region (`aria-live`); there is no separate daemon pill.

### What the parser does with the note

`jarvisd/hub/digestparse.py` imports `GRAMMAR`, `ID_TAIL` and `HEADINGS` from `jarvisd/render.py`, so a
renamed heading fails at import, not in a browser, and reads each section with the contract's regexes. A
note without a `grammar` front matter key (written before the rework) is read with the grammar 1 patterns:
the Brain section becomes Still open and Decided yesterday, the Attention block is derived from the overdue
task and the failing CI lines the note does carry. Every id tail ` [xxxxxxxx]` is removed from the visible
text and kept in a `data-id` attribute. A line that matches no pattern is kept and printed as it is under
its block, and a missing heading counts as zero, so a wording change in the writer degrades to raw lines
instead of an empty page. The Full digest disclosure always holds the whole note.

### Label map

The daemon's vocabulary is translated once, in `label` and the maps around it in `jarvisd/hub/views.py`:

| The daemon says | The page says |
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
| proposed, confirmed, edited_confirmed, rejected | waiting for you, confirmed, confirmed with edits, rejected |
| Runs, Ledger, Reminders, Status | Digest runs, Delivered, due pills, Daemon status |
| consolidation candidates | memory candidates |
| counts_only | counts only, names kept back |
| work_policy | kept back (work metadata) |
| GitHub ci failure / timed_out / success | CI failing / CI timed out / CI green (dropped when the same risk pill is on the row) |
| watermark, heartbeat, kill file, audit head (Daemon status) | Digest window start, Last sign of life, Kill switch, Audit head, as a definition list; the CLI block sits behind "Raw CLI output" |
| RECENT.md age N h, result N (a grammar 1 System line) | notes index updated N h ago, the decoded task result |
| next digest 2026-10-10 06:30 (Activity tile) | Tomorrow 06:30, Today 18:00 |

A code the operator needs for a terminal command (a held id) stays visible; a reason code the map translates
is shown once, in words, and the raw code only when the map has nothing to say.

### Disclosures and the refresh

Collapsing uses native `<details>`. Which ones you opened is remembered per page in your browser's
`localStorage` by `/static/prefs.js` (loaded on every page, with or without the refresh script) and
restored after `/static/hub.js` swaps `<main>`, which announces `hub:refreshed` on the document when it
does. Nothing of that reaches the server. The refresh is a plain `fetch` of the same URL every
`[hub].refresh_s` seconds, paused while the tab is hidden; the Inbox never refreshes itself. The Snooze
disclosure on an item line has no id, so its open state is not remembered.

### Item keys and seen marks

Every Needs you and Still open line carries the item's stable key (`data-key`) and its id (`data-id`). The key is
the one the digest writer put in the run's sidecar `state/runs/<job>/items.json` (the normalised text, contract
section 6); for a note written before the sidecar existed the hub normalises the text itself, so the marks below
still work, but the Done and Snooze buttons need the sidecar and are not drawn without it.

The second block of `/static/prefs.js` keeps one record in `localStorage`, `hub:seen`: the id of the last digest
you looked at and its keys. When Today shows a different digest id (the `data-digest` attribute of the
`<section id="today">`), the previous keys become `prev` and the new ones are stored; a reload or the 30 second
refresh of the same digest stores nothing, so it does not count as a visit. A line whose key is in `prev` gets the
class `seen` and its text is dimmed to 60 percent, still readable: it was already on the last digest you saw.
No storage, nothing dimmed. Nothing of this reaches the server, and the server keeps no record of what you saw.

Held items are references. The page shows the id, the kind and the reason code, and drops
the source path before the data reaches any view (`jarvisd/hub/data.py`), because resolving
an id to its source is terminal-only by design (design D5).

The Projects view runs no git and asks no model. Each row comes from the Repos section of the digest
notes, the proposals folder and the config, so a repo changed since the last digest does not show up until
the next one. The risks are fixed rules, not a judgement: no activity for `[hub].stale_days` days or more
(default 14), uncommitted work for 2 days or more, CI failing, and the active task marked overdue. Activity
is a commit or an uncommitted change seen in one of the last 60 digest notes; a repository whose tree is
always dirty (a notes vault) would be flagged every day, so `[hub].always_dirty` lists the repos that never
get the uncommitted-work risk and are not Active for their dirty counts alone (the names are private, set
them in `jarvis.local.toml`). A repository with no activity in any digest on file counts as idle for as
long as those notes reach back, shown as a number with a plus sign, so the longest idle repositories are
flagged too. The active task is shown under a repository only when its line contains a keyword from
`[hub.task_projects]` in `jarvis.local.toml`; the mapping is private and the tracked file holds none. A
repository the digest has not covered yet reads "no digest data" in the Quiet list; in a grammar 2 note a
configured repository named nowhere is quiet with zero counts.

Delivered (on Activity) lists only what has evidence on disk: a confirmed proposal that carries a tracker
link, a digest run that wrote its note, a consolidation run that wrote its candidates note, a proposals
run that wrote its proposals. A proposal has no cost of its own, a run shows the cost its manifest recorded
(a proposals run's paid call included), and the monthly rollup adds them. It is the delivery record the
spec calls for, not a timesheet.

The audit witness in Digest runs is the audit record that the run's manifest points at. The page looks the
record up in an index built once per audit snapshot and prints its short hash. A manifest that points at a
record the log no longer holds (rotation, pruning) says so instead of showing a hash.

### What a page costs

Each route calls one method of `HubData` (`today`, `projects_page`, `activity`, `status`) that returns
everything its view needs; the views are pure functions and never read a file. The folders read on every
request (held, proposals, run manifests, the newest queue files, the digest notes) are parsed once per
directory signature (name, size and mtime of each file), the way the audit log already was, so
`/api/status`, which the face polls every 5 seconds, reads the three newest files of `queue/done` and
`queue/failed` and nothing else once warm (`tests/test_hub.py` counts the reads). Digest runs is linear in
the number of runs.

## Why it does not simply call the CLI code

Several read paths in the daemon have write side effects that are correct for the daemon:
`JobStore` creates its folders and moves a corrupt job file to `failed/`, `AuditLog.head()`
cuts a half-written last line, and `daemon_running()` creates `state/daemon.lock`. The hub
uses the same models and parsers (`Job`, `RunManifest`, `AuditLog.records` and `verify`,
`Budget.snapshot`, `Breaker.peek`, the CLI's own status formatter) and none of those writers.
Two consequences:

- "Running" on the Status view means a fresh heartbeat from a process that exists, not the
  lock probe the CLI uses. They agree unless the daemon crashed within the last two minutes.
- A corrupt job file is skipped by the hub and quarantined only when the daemon next reads
  the queue.

If the audit file is mid-append when the page is read, the verification is repeated once
before the page says the chain is broken.

## The security model

- Binds `127.0.0.1` only. There is no host setting; `jarvisd/config.py` has no such key.
- A request whose `Host` header is not `localhost`, `127.0.0.1`, `[::1]` or listed in
  `[hub].allowed_hosts` gets 403. That stops a web page on another origin from reading the
  hub through DNS rebinding. A POST also needs an `Origin` that is absent or equals that Host (below).
- Everything is GET except five routes: three on the Inbox (`POST /inbox/<id>/confirm`, `/edit` and `/reject`)
  and two on Today (`POST /today/<id>/done` and `/snooze`); any other method gets 405. All five follow the
  rules in "The write actions" below. There are two write paths, not one: the Inbox decisions in
  `jarvisd/inbox.py` (a proposal file, and a tracker call), and the Today decisions in `jarvisd/attention.py`
  (one file under `state/attention/`, nothing else, no outward call).
- `Content-Security-Policy` allows same-origin CSS and JS only. No inline script or style, no
  CDN, no web font, and the generated API docs (which load from a CDN) are switched off.
- Every value is HTML-escaped, including text read from the vault. The markdown renderer
  supports headings, lists, paragraphs, `**bold**` and `` `code` `` and cannot emit a link,
  an image or raw HTML.
- The hub's own modules import no Claude client, no subprocess and no network library
  (`tests/test_hub.py` checks those direct imports), and `tests/test_write_locations.py` keeps them off
  the list of modules that may open a file for writing. This is not a transitive guarantee:
  `jarvisd/inbox.py` imports `jarvisd/daemon.py` (which imports the Claude client and subprocess) and
  `jarvisd/tracker.py` (which imports `urllib`), so the hub process loads them, and a confirm can reach
  the network through the ClickUp adapter. What holds is that no hub code path calls the Claude client,
  and that the only writes go through `jarvisd/inbox.py` and `jarvisd/attention.py`, both through the
  durable IO of `jarvisd/fsio.py` (`tests/test_write_locations.py` lists the modules that may open a file
  for writing; neither of these is on it).
- No login. The page can only show what is already on this machine, to someone who can already
  reach this machine's loopback port. The Inbox forms carry a per-process token (below), which is a
  defence against other web pages, not a password.

The audit log is shown as it is written, with the plumbing fields (hash chain, pid, version)
left out. Audit records are designed to carry counts, ids and hashes, not content (design
section 13); the page does not add anything to that. The tracker is the one place with a text
exception: a failed ClickUp call stores a scrubbed excerpt of the service's answer (at most 160
characters, token removed) as its error code. A dry run audits `tracker_dry_run` with the list id,
the size and the hash of the request, never its text.

## The write actions

The Inbox is the one place the hub changes anything. `jarvisd/inbox.py` holds the decisions, and
the hub forms and `jarvis proposals confirm` and `jarvis proposals reject` call the same functions,
so the click and the terminal cannot drift apart.

- **Three actions, all POST:** confirm, edit then confirm (title, project and due date only), and
  reject. There is no GET that changes anything and no way to create, delete or run anything else
  from the page.
- **A CSRF token per process.** Every form carries it in a hidden field, made once when the hub
  starts and compared in constant time. A restart invalidates open pages; reload the Inbox. A
  missing or wrong token is a 403 and changes nothing.
- **Origin and Host.** The Host allowlist applies first. Then a POST whose `Origin` is present and
  does not name the same host and port as the `Host` header is a 403, so `http://localhost:<port>`
  and a `tailscale serve` name (once listed in `allowed_hosts`) can decide, and a page from any other
  site cannot. A wrong content type is 415, a body over 16 KB is 413 and a body with more than 20
  fields is 400, all before the token is looked at.
- **Loopback only, as before.** Nothing here adds a listening address.
- **The click is the confirmation.** It is the human confirmation that `[trust].always_confirm`
  asks for. The daemon never creates a task by itself, and nothing but this click and its CLI twin
  calls a tracker adapter.
- **A proposal is decided once.** Anything but `proposed` answers 409 and changes nothing: not the
  file, not the tracker, not the audit chain. The decision runs under one lock that holds across
  processes, so two clicks, or a click and a terminal command, cannot both create the task.
- **The text is gated again at confirm time.** The title, project and rationale as they will be sent
  (your edits applied) go through the tier gate once more, and an evidence id that is in the held store
  now stops the confirm. A term added to `jarvis.local.toml` after the proposal was made, or typed into
  the edit fields, cannot reach a tracker; the page says "sensitive term" and never repeats it. Reject
  the proposal and write the task by hand. Links, images and HTML in the model's text are reduced to
  their words before any adapter sees them.
- **Tracker first, file second.** A tracker failure that certainly created nothing (a refusal, a
  missing token, `dry_run`) leaves the proposal open with its edits unsaved, and the page shows the
  error. A dry-run send is not a confirmation, because no task exists; the page shows the request that
  would have gone out.
- **An unknown outcome blocks a plain second click.** A timeout, a dropped connection, a 5xx, an answer
  that is not a task, or a crash between the call and the save may mean the task exists. The Inbox
  writes `state/proposals/<id>.attempt` before every send and removes it once the outcome is known.
  While it exists the card says so and shows a Confirm anyway button (`--confirm-anyway` on the
  command line); a plain confirm is refused. Look in the tracker first: if the task is there, reject the
  proposal. If the task was created but the file could not be saved, the page says so in plain words,
  and the marker stops a duplicate.
- **A reject needs a reason** (one line, at most 500 characters). It is stored on the proposal,
  and it is what the next proposals run learns from, see [proposals](proposals.md).
- **Every write is audited**: `proposal_confirmed`, `proposal_confirm_failed`,
  `proposal_confirm_override`, `proposal_rejected` and `proposal_reject_failed`, with ids, the adapter
  name and the link, never a title, an edit or the text of a reason. The digest run a proposal came
  from is recorded as `digest_run_id`. The adapters add their own records, see below. The Today decisions
  add `attention_decided` and `attention_decide_failed` ("Done and Snooze on Today").
- **Evidence stays references.** The Inbox shows an evidence id's digest line only if the item was
  cleared. A held id shows the id alone, never a summary, and the adapters refuse any evidence that
  is not an id, so free text cannot ride into a tracker request.
- The Inbox page does not refresh itself, so a half-typed edit is not wiped.

The terminal door:

```
jarvis proposals                                      # open proposals; --all for every state
jarvis proposals confirm <id> [--title T --project P --due YYYY-MM-DD] [--confirm-anyway]
jarvis proposals reject <id> --reason "why"
```

The terminal door is a second path to the same tracker call, so "only a click in the Inbox creates a
task" really means "only a human decision, from the Inbox or from this command".

### Done and Snooze on Today

The second write path, and a much smaller one. Every Needs you and Still open line has a Done button and a Snooze
disclosure (Tomorrow, 3 days, Monday, or a date), both `POST` routes (`/today/<id>/done`, `/today/<id>/snooze`)
handled by `decide_item` in `jarvisd/hub/app.py`, which calls `jarvisd/attention.py`. The terminal twin is
`jarvis attend <id> --done` or `jarvis attend <id> --until tomorrow|3d|monday|YYYY-MM-DD` (`cmd_attend` in the
same module; the CLI wiring is the writer side's).

- **The same guards, in the same order** as the Inbox: Host allowlist, Origin, content type, body size, field
  count, the CSRF token in constant time. Then:
- **The id must be in the latest run's sidecar** (`state/runs/<job>/items.json`), else 404. The item's key is read
  from that record; nothing in the form is trusted beyond the `until` choice. Older notes have no sidecar, so their
  lines carry no buttons and a post is refused.
- **`until`** is `tomorrow`, `3d`, `monday` (the next Monday, never today) or `YYYY-MM-DD` after today and within
  90 days, else 422 and nothing changes.
- **One file per key**, `state/attention/<words>-<8 hex of the key's hash>.json`, holding `key`, `id`, `action`
  (`done` or `snooze`), `until` (a date or null), `decided_at` and `note` (the digest run id), written with
  `atomic_write_text`. That folder is the only thing this path writes. One lock, in-process and cross-process
  (`state/attention/decide.lock`, 20 s); not acquired is 409 "busy".
- **A decision in force is 409** (a done, or a snooze whose date has not passed) and changes nothing; an expired
  snooze is replaced. Two clicks at once end in one file and one audit record.
- **Audited**: `attention_decided` with `item_id`, `action`, `until` and `digest_run_id`; a save failure is 502
  and `attention_decide_failed` with a short error code. Never the key and never the text.
- **Success is 303** to Today with a banner ("Done." or "Snoozed until <date>."), and the line is gone at once:
  `data.today()` reads `state/attention/` and hides every line whose key has a decision in force, with one line
  "N lines decided, applied at the next digest" under Everything else. The Full digest keeps the whole note.
- **What the writer does with it**: the digest reads the folder before rendering (contract section 6.1): a done
  key is excluded for ever, a snoozed key until its date, after which the line returns marked "back from snooze"
  in Changed since yesterday. A thread whose text changes has a new key and comes back at once.

## Where a confirmed proposal goes

`[tracker].adapter` picks one of two adapters (`jarvisd/tracker.py`). Both are built and tested
offline, and are called by one thing only: a confirm, from the Inbox or from the terminal.

| Adapter | What it does | Leaves the machine? |
|---|---|---|
| `markdown` (default) | Appends a block to `raw/jarvis/confirmed-tasks.md` in the notes vault, through the single vault writer, and returns a `file:` link | No |
| `clickup` | Creates a task with one POST to the ClickUp REST API (`/list/<id>/task`) | Yes: title, project, rationale, evidence ids and due date of that one proposal |

Check readiness without sending or writing anything:

```
jarvis tracker check
```

It prints the active adapter and whether it is ready, then the state of both: whether the markdown
file is writable, whether a ClickUp token is present (yes or no, never the value), how many projects
the list map holds, and whether `dry_run` is on.

### Setting up ClickUp

1. Create a personal API token in your ClickUp account settings.
2. Put the token in a user environment variable, for example `JARVIS_CLICKUP_TOKEN`. The name is the
   default of `[tracker].clickup_token_env`; change that key if you use another name. The token is
   read from the environment at the moment of each request, so a token set after the hub started
   is picked up. It is never read from a file, never logged and is scrubbed from error text.
3. In `jarvis.local.toml` (gitignored, and the only place real ids belong), select the adapter and map
   project names to list ids:

   ```toml
   [tracker]
   adapter = "clickup"
   clickup_token_env = "JARVIS_CLICKUP_TOKEN"

   [tracker.clickup]
   default_list_id = "901999"      # used when a project has no entry below
   # status = "to do"              # empty: the list's default; statuses differ per space
   dry_run = true                  # start here
   timeout_s = 15.0

   [tracker.clickup.lists]
   example-api = "901100"          # project name as in the proposal, to a list id
   ```

   The ids above are synthetic; see `jarvis.local.toml.example`. A list id is the number in the list's
   URL or in its "copy ID" menu. Project names are compared without regard to case. Digits only: a
   value that is not a list id is refused when the configuration loads.
4. Run `jarvis tracker check`. With `dry_run = true` no token is needed: a confirm builds the exact
   request, shows it on the Inbox page (or in the terminal) and sends nothing, so you can read what
   would be sent. The audit log keeps only its size and hash. Set it to false only after that looks right.

Behaviour worth knowing before the first real send:

- A create is never retried, because a retry could duplicate the task. A failure is shown to you and
  you decide; when the outcome is unknown, the Inbox refuses a plain second confirm (see above).
- Redirects are refused, so the token cannot travel to another host. `api_base` must be https unless
  the host is loopback (that is how the tests run).
- A bare due date is sent at 12:00 UTC without a time of day, so it lands on the same calendar day in
  any workspace from UTC-12 to UTC+11.
- The audit log gets `tracker_intent` (adapter, proposal id, list id, size and hash of the request)
  before a send and `tracker_result` (ok, external id, error code, whether the outcome is unknown, HTTP
  status) after it. A dry run gets `tracker_dry_run` with the same ids, size and hash.
- The ClickUp adapter has only met a local stand-in server. Whether your workspace accepts the
  status and the fields is unproven until you try it with `dry_run` and then once for real.
- The markdown adapter appends under an in-process lock only. A second process appending to the same
  file at the same moment is not guarded against.
- A proposal's `tracker_ref` accepts an http, https or `file:` link, so both adapters can record
  theirs. The Ledger turns only http and https links into anchors; a `file:` link is shown as text.

## The face

`/face` is the avatar's home: one `<lantern-puppet>` (the paper-lantern character from the `lantern-avatar`
project) filling the window on a dark page, with its current state written underneath. It is meant for a small
always-on window next to your work: `bin/face-window.cmd` opens it in an app-mode Edge (Chrome if Edge is
missing) of 420 by 460 pixels, on the port in `[hub].port` (falling back to 8765, or pass a port as the first
argument). The hub has to be running (`jarvis hub`, or `bin/hub.cmd` once, which starts it). The "JARVIS
Face" shortcut from `deploy/make-shortcuts.ps1` runs this launcher.

The same puppet is also on every hub view, as the companion: on a wide screen (88rem and up) a 10rem lantern
with its state under it, fixed in the right margin next to whatever you are reading; below that a 2.75rem
link docked at the right end of the status strip (the puppet inside it shrinks to 1.75rem under 40rem, the
hit box does not). It sits outside `<main>`, so the page refresh never
resets it, and clicking it opens `/face`. The companion appears only when the assets are installed; otherwise
the views are as before. The same poll that moves the avatar repaints the status strip.

Where the files come from. `[hub].face_dir` (default `~/lantern-avatar`) is read, never written, and only these
names are served under `/static/face/`: `lantern.js`, `lantern-puppet.js`, `body/poses.json` and `body/<name>.png`
(letters, digits, dot, dash and underscore; no sub-folders). Anything else, a `..` in any spelling, a backslash, a
drive letter, or a link that leads out of the folder, answers 404. The page's own stylesheet and script
(`/static/face.css`, `/static/face.js`) are constants in `jarvisd/hub/face.py`, like the other views. If the
folder or its two scripts are missing, `/face` answers 200 with a plain "avatar assets are not installed" page.

The page follows the same boundaries as the rest of the hub: loopback only, the Host guard, GET only, and the
Content-Security-Policy unchanged (external module scripts and CSS, images from the hub itself, no inline code,
no CDN, no font download). The two components paint themselves with constructable stylesheets
(`adoptedStyleSheets`), which `style-src 'self'` allows; an inline `<style>` in their shadow root would be blocked.

Port. The hub defaults to 8765, which is also the port the lantern-avatar sheet is usually served on
(`python -m http.server 8765` in that folder). Set `[hub].port` in `jarvis.local.toml` (this machine uses 8791)
so `face-window.cmd`, which reads the same config, opens the hub and not the sheet server.

State mapping. The page fetches `/api/status` every 5 seconds and picks the first row that matches:

| Avatar state | When |
|---|---|
| sad | the kill file is present, the daemon is paused, or the breaker is not closed (the daemon is disabled) |
| thinking | a job is in `queue/running` |
| sad | the newest finished job (done or failed) failed |
| sleepy | the daemon is not running (no fresh heartbeat) |
| listening | at least one proposal is waiting in the Inbox (`status: proposed`), by day |
| sleepy | between 23:00 and 07:00 local time, or nothing in the status has changed for 30 minutes while the daemon is otherwise idle |
| idle | the daemon is healthy and quiet |

A waiting proposal keeps the avatar listening however long it waits (the 30 minute rule only replaces idle), and
at night sleepy replaces both idle and listening. When the newest finished job is a new, successful one since the
previous poll, the avatar reacts happy for 2 seconds on top of whatever state it settles into (the first poll
after the page opens never reacts). If the poll fails, the avatar is confused and the label says the hub cannot
be reached; it recovers on the next good poll.

The two facts the CLI status does not carry are added to `/api/status` under `face`: `inbox_pending` (count of
proposals still `proposed`) and `last_finished` (`id`, `state` and `at` of the newest job in `queue/done` or
`queue/failed`, from the three newest files of each, so the cost does not grow with the queue). The `jarvis
status` output is unchanged. The mapping is a pure function, `mapState` in `jarvisd/hub/face.py`, and
`tests/test_hub_face.py` runs it under node.

## Opening it, keeping it running

`bin/hub.cmd` opens the hub at `http://127.0.0.1:<port>/` in an app-mode window (Edge, or Chrome) of 1180 by
820 pixels. When nothing listens on the port it starts the hub first: a scheduled task named JarvisHub when
one is registered, else `pythonw -m jarvisd hub` detached from the window, and it waits up to 20 seconds for
the port before opening the browser. The port is `[hub].port` read the way the CLI reads it, or the first
argument. Under pythonw the hub's own output goes nowhere; a crash lands in `logs/jarvisd-crash.log` like
the daemon's, and a port already taken (another hub, the lantern-avatar sheet server on 8765) ends the
process with exit code 1, so give the hub its own port in `jarvis.local.toml`.

`deploy/make-shortcuts.ps1` writes two shortcuts, "JARVIS Hub" (`bin/hub.cmd`) and "JARVIS Face"
(`bin/face-window.cmd`), on the Desktop and under Start menu, Programs, JARVIS, with the lantern icon
`bin/jarvis.ico` and the launcher window minimised. `-Remove` deletes them.

A hub started this way lives until logout or reboot. To have it back at every logon, register a task named
JarvisHub on the model of `deploy/register-jarvisd-task.ps1`: same user, Interactive logon, RunLevel Limited,
no time limit, restart on failure, logon trigger only, and the action `pythonw -m jarvisd hub` in the
checkout. `bin/hub.cmd` runs that task by name when it exists. The kill switch does not know this task on
purpose: the hub reads state and writes nothing outside the Inbox decisions, so it may stay up while the
daemon is stopped (the face then shows the daemon as sad or sleepy).

## Reaching it from a phone

Keep the hub on loopback and let Tailscale proxy to it, on the port the hub listens on (`[hub].port`,
8765 by default):

```
tailscale serve --bg 8765
```

`tailscale serve status` shows the mapping; it survives reboots and keeps pointing at the old port after a
`[hub].port` change, so run `tailscale serve reset` and the command above again with the new port. The hub
has to be alive for the proxy to answer, and a device sees it only while it is on the tailnet; `serve` never
publishes to the internet (that would be `tailscale funnel`, which the hub is not written for).

Tailscale forwards the tailnet name in the `Host` header, so the hub refuses it until you
list the name. That name is private, so it goes in `jarvis.local.toml`, never in the tracked
`jarvis.toml`. Once listed, the Inbox buttons work from the phone too: the browser's `Origin` is the
same name, which is what the POST check wants.

```toml
[hub]
allowed_hosts = ["machine.example-tailnet.ts.net"]
```

Restart `jarvis hub` after changing it. Bare host names only: no scheme, port or path.

## Configuration

`[hub]` in `jarvis.toml`:

| Key | Default | Meaning |
|---|---|---|
| `port` | 8765 | listen port, 1024 to 65535 |
| `refresh_s` | 30 | an open page fetches itself again this often; 0 removes the script |
| `audit_rows` | 50 | rows in the Audit records disclosure on Activity |
| `allowed_hosts` | none | extra `Host` names, see above |
| `stale_days` | 14 | Projects view: stale badge once a repository shows no commit or uncommitted change in the digest notes for this many days (1 to 365) |
| `face_dir` | `~/lantern-avatar` | folder the `/face` page serves the avatar from, see "The face" |
| `task_projects` | none | Projects view: repository name to keywords that tie the active task to it; set it in `jarvis.local.toml` |
| `always_dirty` | `[]` | Projects view: repositories whose tree is always dirty; they never get the uncommitted-work risk and their dirty counts alone do not make them Active. Names are private: set it in `jarvis.local.toml` |

`[tracker]` and `[propose]` have their own tables, see "Where a confirmed proposal goes" and [proposals](proposals.md).

## Dependencies

The hub is the only part of the project that needs more than pydantic, APScheduler and
pytest. Pinned in `requirements.lock` and installed by `deploy/setup-venv.ps1`:

| Package | Why |
|---|---|
| `fastapi` | the application and router (brings `starlette` and `annotated-doc`) |
| `uvicorn` | the server `jarvis hub` starts (brings `click` and `h11`) |
| `httpx2` | required by `starlette.testclient`, which the tests and `jarvis hub --check` use (brings `httpcore2` and `truststore`) |

The daemon itself does not import any of them; `jarvis hub` loads them when it runs, so every
other command works on an install without them. `httpx2` is the successor Starlette asks for
in place of `httpx`. `opentelemetry-api` is pulled in by the pinned `fastapi` release.

## When something looks wrong

- Every view fails with 403: the `Host` header is not loopback. Use `http://127.0.0.1:8765/`
  or add the name to `allowed_hosts`.
- A button answers "Origin not allowed": the page was opened through an address whose Origin differs
  from the Host the request reached (some proxies rewrite one of them). Open the hub directly, or make
  the proxy pass the Host through.
- A card says the outcome of an earlier attempt is unknown: see "An unknown outcome blocks a plain
  second click" above.
- "Stopped." in the strip while the scheduled task is up: the heartbeat is older than two
  minutes or its process is gone. `jarvis status` asks the lock instead.
- "Chain BROKEN": read the incident runbook in `docs/v1-design.md`, section 16.
- The port is taken: another hub is running. Use `--port`.
- `jarvis tracker check` says "not ready": it names the missing piece, a token in the environment variable
  or a list id in `[tracker.clickup]`. The markdown adapter is not ready only when the vault writer refuses
  `raw/jarvis/confirmed-tasks.md`.
