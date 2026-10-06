# The work hub

What this is: a small web page served from your own machine that shows what the JARVIS
daemon has already recorded, and, in the Inbox, lets you confirm, edit or reject the task
proposals of [proposals](proposals.md). Every view except the Inbox is a viewer: it reads the
daemon's files and never writes one, never takes a lock, never calls Claude and never starts a job.
The Inbox is the one exception, and only on a click: see "The write actions".

```
jarvis hub                 # serve on http://127.0.0.1:8765/ until Ctrl+C
jarvis hub --port 9000     # another port (1024 to 65535)
jarvis hub --check         # render every view once against the real state, then exit
```

## Status, stated plainly

- **Built and tested:** the read-only views (Today, Runs, Held, Repos, Projects, Ledger, Reminders, Audit,
  Status), the Inbox with its three POST routes, the loopback, Host and Origin guards, the CSRF
  token, the read-only data layer and `jarvis hub --check`. The suites are `tests/test_hub.py`,
  `tests/test_hub_projects.py` and `tests/test_inbox.py`. They drive the real application through
  FastAPI's `TestClient` over a throwaway tree that the daemon's own writers filled, and
  `tests/test_hub.py` compares the whole tree byte for byte before and after every read-only view.
  The Inbox tests use a fake tracker, or the markdown adapter on a throwaway vault.
- **Never done for real:** a confirm or a reject outside the tests, a confirm against the live
  ClickUp service, and a confirm of a proposal made by the real model (no paid proposals run has
  been made). Everything the Inbox does has met only a fake tracker, the markdown adapter on a
  throwaway vault and a local stand-in for ClickUp.
- **Not built:** pushed reminders (ntfy), ClickUp due dates in the Reminders view, local triage of events, Slack data, a SQLite index,
  authentication, TLS. The page has no login because it listens on loopback only.
- **Tested against fakes only:** the ClickUp adapter has never talked to the real ClickUp service,
  only to a local stand-in server. Use `dry_run` first (below).
- **Not verified on a real second device:** the `tailscale serve` steps below were written
  from the Tailscale documentation and have not been run against this application. Check
  the flags with `tailscale serve --help`.
- **Not looked at in a browser by the author of the tests.** The tests prove the HTML,
  headers and data; they cannot prove the layout looks right. Open it once.

## The views

| View | What it shows | Where the data comes from |
|---|---|---|
| Today | The latest digest note rendered from markdown, its front matter as facts, and the held references by id, kind and reason | the digest notes in the vault's `raw/jarvis` folder, `queue/held/` |
| Runs | One row per digest job: date, status, item counts, cost, audit witness, link to the note | `state/runs/<job>/run.json`, `queue/*/`, the audit log |
| Held | The held references and what to run in a terminal (`jarvis held`, `jarvis wrong <id>`, `--leak`) | `queue/held/` |
| Repos | The Repos section of the latest digest, one row per repository | the same digest note |
| Projects | One row per repository in `[digest].repos`: branch, commits, uncommitted files, open pull requests, CI, days idle, risks, open proposals | the digest notes, `state/proposals/`, the config |
| Ledger | What was delivered, newest first, and a rollup per month: confirmed proposals with their tracker link, digests written, consolidation notes, proposals runs, with the recorded cost | `state/proposals/`, `state/runs/<job>/run.json`, the digest notes |
| Inbox | The proposals waiting for a decision, with confirm, edit and reject forms; each evidence id shows its digest line if it was cleared, and the id alone if it was held | `state/proposals/`, the digest note of the proposal's run |
| Reminders | Due dates of confirmed proposals and of proposals still in the Inbox, grouped overdue, today, next 7 days, later; an edited due date beats the model's hint | `state/proposals/` |
| Audit | Chain verification result, the newest events (default 50), today's budget | `logs/jarvisd-audit*.jsonl`, `state/budget.json` |
| Status | The same lines `jarvis status` prints, plus the queue counts | `state/`, `queue/`, the audit log |

`/api/status` returns the Status data as JSON. `/digest/<job id>` shows one digest note;
the Runs table links to it.

Held items are references. The page shows the id, the kind and the reason code, and drops
the source path before the data reaches any view (`jarvisd/hub/data.py`), because resolving
an id to its source is terminal-only by design (design D5).

The Repos view does not run git. It shows what the collector found when the last digest was
built, so a repo changed since then does not show up until the next digest.

The Projects view runs no git and asks no model either. Each row comes from the Repos section
of the digest notes, the proposals folder and the config. The risks are fixed rules, not a
judgement: no activity for `[hub].stale_days` days or more (default 14), uncommitted work for 2 days
or more, CI failing, and the active task marked overdue. Activity is a commit or an uncommitted
change seen in one of the last 60 digest notes; the page reads no git and no reflog, so a repository
whose tree is always dirty counts as active (and is flagged as uncommitted work instead). A
repository with no activity in any digest on file counts as idle for as long as those notes reach
back, shown as a number with a plus sign, so the longest idle repositories are flagged too. The active task is shown under a repository only when its line
contains a keyword from `[hub.task_projects]` in `jarvis.local.toml`; the mapping is private
and the tracked file holds none. A repository the digest has not covered yet reads "no digest
data".

The Ledger lists only what has evidence on disk: a confirmed proposal that carries a tracker
link, a digest run that wrote its note, a consolidation run that wrote its candidates note, a
proposals run that wrote its proposals. A proposal has no cost of its own, a run shows the cost its
manifest recorded (a proposals run's paid call included), and the monthly rollup adds them. It is the delivery record the spec calls for, not a timesheet.

The audit witness in Runs is the audit record that the run's manifest points at. The page
looks the record up and prints its short hash. A manifest that points at a record the log no
longer holds (rotation, pruning) says so instead of showing a hash.

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
- Everything is GET except three Inbox routes (`POST /inbox/<id>/confirm`, `/edit` and `/reject`),
  and any other method gets 405. Those routes follow the rules in "The write actions" below.
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
  and that the only writes go through `jarvisd/inbox.py`.
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
  from is recorded as `digest_run_id`. The adapters add their own records, see below.
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

## Reaching it from a phone

Keep the hub on loopback and let Tailscale proxy to it:

```
tailscale serve --bg 8765
```

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
| `audit_rows` | 50 | rows on the Audit view |
| `allowed_hosts` | none | extra `Host` names, see above |
| `stale_days` | 14 | Projects view: stale badge once a repository shows no commit or uncommitted change in the digest notes for this many days (1 to 365) |
| `task_projects` | none | Projects view: repository name to keywords that tie the active task to it; set it in `jarvis.local.toml` |

`[tracker]` and `[propose]` have their own tables, see "Where a confirmed proposal goes" and [proposals](proposals.md).

The refresh is a plain `fetch` of the same URL and a swap of the page body; it pauses while
the tab is hidden.

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
- "daemon not running" while the scheduled task is up: the heartbeat is older than two
  minutes or its process is gone. `jarvis status` asks the lock instead.
- "Chain BROKEN": read the incident runbook in `docs/v1-design.md`, section 16.
- The port is taken: another hub is running. Use `--port`.
- `jarvis tracker check` says "not ready": it names the missing piece, a token in the environment variable
  or a list id in `[tracker.clickup]`. The markdown adapter is not ready only when the vault writer refuses
  `raw/jarvis/confirmed-tasks.md`.
