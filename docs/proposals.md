# Task proposals

What this is: a job that reads the same work items the morning digest reads, asks Claude once
for a short list of task proposals, and saves each one as a file. A proposal is a suggestion
with evidence. It becomes a task in a tracker only when you confirm it, with a click in the hub
Inbox or with `jarvis proposals confirm` in a terminal. That human decision, by either door, is the
only outward action; nothing else creates a task (see [hub](hub.md)).

```
jarvis propose --dry-run   # print the exact payload and the held list; no call, no write, no job
jarvis propose             # one real run; needs [propose].enabled = true, or --force once
jarvis proposals           # list open proposals; --all adds confirmed, edited and rejected ones
jarvis proposals confirm <id> [--title T --project P --due YYYY-MM-DD] [--confirm-anyway]
jarvis proposals reject <id> --reason "why"
```

The last two do what the Inbox buttons do, through the same code. `--confirm-anyway` is the
explicit override after an earlier attempt whose outcome is unknown, see [hub](hub.md).

## Status, stated plainly

- **Built and tested offline:** the job (`jarvisd/propose.py`), the reply checks, the proposal
  files, the daemon chaining, the commands above, the `Proposal` model and the decisions
  (`jarvisd/inbox.py`). The tests (`tests/test_propose.py`, `tests/test_inbox.py`) use a scripted
  Claude double, a fake tracker and a throwaway state tree.
- **Never run against the real model.** Only the dry run has been done. There is no measured
  cost, no measure of how good the proposals are, and no count of how many a typical day yields.
  Read the dry run before the first paid one, as with the digest.
- **Off by default** (`[propose].enabled = false`): a run is one paid call a day.
- **No proposal made by the real model has been confirmed or rejected**, because none has been made. The confirm and reject paths ran only in tests.

## How a run works

1. **Anchor.** The newest digest run whose manifest says `complete` fixes the run id. The time
   window is the digest job's, widened to at least `[digest].window_hours_default` hours, because a
   forced digest rerun starts at the watermark and its own window can be a few minutes. With no
   complete run the job ends as `no_digest` and calls nothing.
2. **Collect again.** Only the local sources run: the notes vault, the active task, local git and
   the GitHub read. The ClickUp section is never run (it makes a paid call of its own) and the
   deterministic system lines are never sent.
3. **Gate.** Every item goes through the same gates as the digest: the tier gate, the router,
   then the work-metadata policy. The digest manifest holds counts and hashes, not item ids, so
   "cleared" is decided again here, with the same code, on what the sources say now.
4. **Seal.** `clear_for_claude` builds the only payload type the Claude bridge accepts. If the
   sealing step finds a sensitive term, the job fails, the breaker opens for a human and nothing
   is sent.
5. **Ask.** One call through `jarvisd/claude.py`: isolated argv, no tools, the daily ledger
   (purpose `propose`) and the breaker. The system prompt is a reviewed constant, not a setting.
6. **Check.** The reply is untrusted data. Each element is validated and cleaned, and unsafe or
   ungrounded ones are dropped (next section).
7. **Write.** Each survivor is saved atomically as `state/proposals/<id>.json`, with a manifest
   and audit records that carry ids and counts only.

The daemon queues the job right after a complete digest when `[propose].run_after_digest` is
true, and again on every tick, once per date. `jarvis propose` runs it by hand. A retry of the
same job derives the same ids, and a proposal file that already exists is never rewritten: the
owner may have rejected it between two attempts, and a rewrite would undo that and lose the reason.
A paid answer whose write failed is kept in `state/runs/<job>/proposals-draft.json` and reused on
the next attempt.

## What enters the payload, and what never does

| Enters | Never enters |
|---|---|
| Items whose gate result routes to Claude: notes, the active task, git and GitHub facts | Held items (the tier gate, or the work policy): referenced by opaque id and reason code only, never summarized |
| The last 20 rejected proposals as short negative examples (title, reason), after the same gates | Anything from the ClickUp section, the system lines, or a file the tier gate refused to open |
| The 30 newest live proposals (open, confirmed or edited) as rows with an `open-` id: title and a short rationale, after the same gates, so the model does not re-propose standing work in new words | The `rejected-` and `open-` rows as evidence: they are never valid citations |
| A header with the date, the window and the maximum number of proposals | Free text a proposal later carries into a tracker: that text comes from the checked reply, not from the items |

On a machine where the work policy holds repository facts (`work_metadata_to_claude = false`),
git and task items are held, so the payload is mostly notes. That is the policy working, not a
fault.

The checks on the reply, all in code:

- It must be a JSON array. An element with an unknown key, a wrong type, a bad `kind` or a due
  date that is not `YYYY-MM-DD` is dropped alone.
- Every evidence id must be an id that was really sent as a work item. One id outside that set
  drops the whole proposal, and so does citing a rejected example or an open proposal.
- Text is flattened to one line, stripped of dashes, tags, wikilink brackets and Markdown links and
  images (the words stay, the target goes, because a note opened in Obsidian would fetch a remote
  image), clipped to its limit and scanned again for sensitive terms.
- The id, creation time, run id and status are assigned by the program, never read from the reply.
- A title that matches a live proposal (proposed, confirmed or edited, compared with case, accents
  and punctuation folded) is dropped as a duplicate. Rejected ones are not compared on purpose.
  Matching titles cannot catch the same work in other words; that is what the `open-` rows are for,
  and it stays a request to the model, not a guarantee.
- At most `[propose].max_proposals` survive (default 8).

## The rejection feedback loop

Rejecting a proposal, in the Inbox or with `jarvis proposals reject`, requires a reason (one line,
at most 500 characters) and stores it on the proposal (`rejected_reason`, status `rejected`). The next run turns the 20 newest rejected proposals into items with ids that start
with `rejected-`. They take the same gates as everything else, so a reason that names a sensitive
term is held and simply not sent. The prompt tells the model to propose nothing like them and
never to cite them as evidence, and the reply check enforces the second half.

This is guidance, not a block. The model may propose a similar title again, and nothing counts
rejections or tunes a threshold. The weekly review of the spec is a human reading the list. An
edit and confirm is recorded as `edited_confirmed` with the changes under `edits`; edits are not
fed back.

## Cost

Three limits apply before the call is spawned: `[propose].max_budget_usd` (default 0.30, at most
1.00) caps the one call and replaces `[claude].max_budget_usd` for it, the shared daily ledger
(`daily_budget_usd`, `daily_calls`) counts it with the digest, and the breaker can refuse it. A run
that finds nothing to send, or no digest, makes no call and costs nothing. A `--dry-run` costs
nothing and records only audit lines.

The only measured Claude cost in this project is the digest's, in the
[first-run log](first-run-log.md). No proposals run has been priced. Expect a larger payload than
the digest's small one, so more than that figure, and use `jarvis audit cost --days 7` to see what
your own runs settled.

## Configuration

`[propose]` in `jarvis.toml`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | false | allow the scheduled and the plain `jarvis propose` run |
| `max_proposals` | 8 | most proposals one run keeps (1 to 20) |
| `max_budget_usd` | 0.30 | cap of the one call, passed as `--max-budget-usd` (at most 1.00) |
| `run_after_digest` | true | queue the job after a complete digest, when enabled |

`--force` means two things: run although `enabled` is false, and run again on a date that
already has a job (it writes `propose-<date>-r2`). `--dry-run` needs neither. Where a confirmed
proposal goes is set by `[tracker]`, see [hub](hub.md).

## When something looks wrong

- "No complete digest run": run the digest first, or check `jarvis status`.
- Every proposal is dropped as ungrounded: the dry run shows which ids were sent; the model must
  copy ids from it. A model that invents ids is working as the check expects.
- `jarvis propose` exits 3 with a budget or breaker message: the shared ledger or the breaker
  refused it, see `jarvis audit tail` and [operations](v1-operations.md).
- A proposal you expected is missing: it may have been held. The held list of the dry run names
  the ids, kinds and reason codes, and `jarvis held <id>` resolves one in the terminal only.
