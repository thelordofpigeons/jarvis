# Consolidation: nightly memory candidates (spec 6a)

Status: built and tested against the fake `claude` binary. Never run against the real
model in this repository's test suite, and off by default. Read "What is not proven" before
you switch it on.

## What it does

Once a night JARVIS reads the session notes of the window, asks Claude once for at most
eight insights worth keeping, and writes them to `brain/raw/jarvis/candidates-<date>.md`.
You read that note and promote what you agree with, by hand or with your own promotion
ritual. JARVIS never writes to `insights/`, `telos/` or `sessions/`: the vault writer
refuses those places and this module never asks.

This automates the part of memory upkeep that is tedious (re-reading a day of notes to spot a
decision or a recurring problem) and leaves the part that matters (deciding what is true and
worth keeping) with you.

## Turn it on

```toml
# jarvis.toml already ships this table, switched off
[consolidate]
enabled = false
run_at = "02:00"
max_candidates = 8        # 1 to 8
```

Put `enabled = true` in `jarvis.local.toml`. Four more keys have code defaults:
`window_hours_default` (36), `window_hours_max` (72), `max_notes` (30) and
`max_lines_per_note` (80). The daemon reads the config on every tick, so no restart is needed.

Before the first real night, run the dry run and read it:

```
jarvis consolidate --dry-run
```

It prints the exact payload that would go to Claude, the held list (ids and reason codes, never
content) and spawns nothing. Add a missed term to `[gates].sensitive_terms` if something
private is in the payload. Then a single paid run by hand:

```
jarvis consolidate --claude
```

Without `--claude` or `--dry-run` the command does nothing and says so. `--date` picks the
day, `--force` writes `candidates-<date>-r2.md` when a note for the date exists.

## How it is wired

1. `reconcile_consolidation` runs on every daemon tick, after the digest's reconcile. At or
   after `run_at` it enqueues `consolidate-<local date>`, once per date. A machine that was
   off at 02:00 runs the job when it next ticks that day.
2. The job reads `brain/sessions/*.md` newer than the window start, which is the end of the
   last note that was written (`state/consolidate-watermark.json`), capped at
   `window_hours_max`. It reuses the brain collector's listing and the same read primitive
   (`tier.safe_read_text`), so a note with a sensitive tag, flag or forbidden path is
   withheld whole and unread.
3. Each remaining line is scanned for `[gates].sensitive_terms` on its own: a hit holds that
   line only. A hit in a heading holds the section under it (the whole note, for a title).
   Front matter, headings, code fences and the "Files changed" section are never sent.
4. Lines become items and go through the digest's gates (`dispatch.run_gates`, tier first,
   then the router) and the sealing step (`clear_for_claude`). The payload is capped by
   `[digest].max_payload_bytes`, newest notes first.
5. One call through `ClaudeClient.complete`, the only code that spawns `claude`: the same
   isolated argv (no tools, no settings, no MCP, no session file), the same budget ledger
   (purpose `consolidate`), breaker, kill file and isolation checks, with its own constant
   system prompt and reply parser. It counts toward `daily_calls`, so a night costs one call
   next to the digest's one to three.
6. The reply is treated as data. Evidence must name a note and a line that were really sent,
   otherwise it is dropped, and a candidate with no evidence left is dropped. Text is flattened
   to one line, stripped of dashes, wikilink brackets and heading marks, clipped, and scanned
   for sensitive terms again. The count is capped at `max_candidates`.
7. The note is written through `VaultWriter.write_raw`, marked `generator: jarvisd`. The job
   emits `consolidate_done` (counts and a path) or `consolidate_failed` (an error code) to the
   audit log, and the morning digest's "What JARVIS did while you slept" shows one line for it.

Failures: a transient error asks for a retry (10 then 30 minutes); a network outage waits
without costing an attempt; an auth, rate limit, budget or breaker refusal fails the job and
writes nothing; a busy vault keeps the paid answer in `state/runs/<job>/candidates.json` and
reuses it on the next attempt.

## What is not proven

- The behaviour against the real model. Tests drive the real client into a fake binary that
  returns canned JSON, so the argv, budget, breaker, parsing and file writes are exercised, and
  the quality of the candidates is not. Judge the first few notes yourself before relying on it.
- The privacy model is the digest's: heuristic for untagged personal content. A line that no
  rule catches reaches Claude. The dry run exists so you can see that before it happens.
- Candidates are a model's reading of a few lines. The evidence refs let you check each one
  against the source line; nothing checks that the inference is sound.
- Only lines are read, not whole notes, and the payload is capped, so on a busy window older
  notes may not fit. The note's front matter says how many lines were sent and held.

## Where the note goes

The path is flat, `raw/jarvis/candidates-<date>.md`. The design reserved a
`raw/jarvis/candidates/` folder for this; the vault writer allows both, and the flat name was
chosen so a file listing of `raw/jarvis/` shows digests and candidates side by side.
