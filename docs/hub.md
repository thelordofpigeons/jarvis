# The work hub

What this is: a small web page served from your own machine that shows what the JARVIS
daemon has already recorded. It is a viewer. It reads the daemon's files and never writes
one, never takes a lock, never calls Claude and never starts a job.

```
jarvis hub                 # serve on http://127.0.0.1:8765/ until Ctrl+C
jarvis hub --port 9000     # another port (1024 to 65535)
jarvis hub --check         # render every view once against the real state, then exit
```

## Status, stated plainly

- **Built and tested:** the six views, the loopback and Host-header guards, the read-only
  data layer, `jarvis hub --check`. The suite is `tests/test_hub.py`. It drives the real
  application through FastAPI's `TestClient` over a throwaway tree that the daemon's own
  writers filled, and it compares the whole tree byte for byte before and after every view.
- **Not built:** any action from the page (confirming, rejecting or editing anything), the
  Inbox and Projects views of the original hub idea, a SQLite index, ClickUp or Slack data,
  authentication, TLS. The page has nothing to authenticate because it cannot change
  anything, and it listens on loopback only.
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
| Audit | Chain verification result, the newest events (default 50), today's budget | `logs/jarvisd-audit*.jsonl`, `state/budget.json` |
| Status | The same lines `jarvis status` prints, plus the queue counts | `state/`, `queue/`, the audit log |

`/api/status` returns the Status data as JSON. `/digest/<job id>` shows one digest note;
the Runs table links to it.

Held items are references. The page shows the id, the kind and the reason code, and drops
the source path before the data reaches any view (`jarvisd/hub/data.py`), because resolving
an id to its source is terminal-only by design (design D5).

The Repos view does not run git. It shows what the collector found when the last digest was
built, so a repo changed since then does not show up until the next digest.

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
  hub through DNS rebinding.
- GET only: any other method gets 405.
- `Content-Security-Policy` allows same-origin CSS and JS only. No inline script or style, no
  CDN, no web font, and the generated API docs (which load from a CDN) are switched off.
- Every value is HTML-escaped, including text read from the vault. The markdown renderer
  supports headings, lists, paragraphs, `**bold**` and `` `code` `` and cannot emit a link,
  an image or raw HTML.
- The module imports no Claude client, no subprocess and no network library
  (`tests/test_hub.py` checks the imports), and `tests/test_write_locations.py` keeps it off
  the list of modules that may open a file for writing.
- No token. The page can only show what is already on this machine, to someone who can
  already reach this machine's loopback port.

The audit log is shown as it is written, with the plumbing fields (hash chain, pid, version)
left out. Audit records are designed to carry counts, ids and hashes, not content (design
section 13); the page does not add anything to that.

## Reaching it from a phone

Keep the hub on loopback and let Tailscale proxy to it:

```
tailscale serve --bg 8765
```

Tailscale forwards the tailnet name in the `Host` header, so the hub refuses it until you
list the name. That name is private, so it goes in `jarvis.local.toml`, never in the tracked
`jarvis.toml`:

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
- "daemon not running" while the scheduled task is up: the heartbeat is older than two
  minutes or its process is gone. `jarvis status` asks the lock instead.
- "Chain BROKEN": read the incident runbook in `docs/v1-design.md`, section 16.
- The port is taken: another hub is running. Use `--port`.
