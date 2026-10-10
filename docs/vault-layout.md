# Vault layout: what JARVIS reads and what it writes

Status: built, tested on synthetic vaults, and run for real on the author's vault. The layout is
the author's own conventions, written down so you can tell in two minutes whether your notes will
produce a useful digest or an empty one.

JARVIS reads a folder of Markdown notes (the "vault", `~/brain` by default, see `[paths]` in
`jarvis.toml`). It never needs the whole vault: the brain collector looks at exactly three things.

## What is read

| Where | What JARVIS looks for | What it becomes |
|---|---|---|
| `RECENT.md` | Bullets of the form `- [YYYY-MM-DD] text` under a `## Open Threads` heading, and under `## Recent Decisions` (or `## Decisions`) | Open-thread and decision items. At most 5 bullets per section, 400 characters each |
| `sessions/YYYY-MM-DD-*.md` | Session notes newer than the digest window. The sections `## Next session entry point` (a sentence or bullets) and `## Open threads` (bullets). Files named `jarvis-*.md` are skipped | One item per line of those two sections |
| `session-checkpoints/*.json` | Only the number of files | A count, nothing is opened |

`RECENT.md` is expected to be rebuilt by something else, nightly (the author's notes tooling does
it). If it is older than 30 hours the digest says so, and the morning job waits up to 90 minutes
for a fresh one before it runs anyway.

Never read, by design: `telos/`, `notes/`, `insights/`, `raw/` (other than the digest it wrote
itself), and anything under a path, tag or term you marked sensitive. The first two folder names
are a fixed floor; add your own with `[paths].vault_forbidden` and `[gates]` in
`jarvis.local.toml`.

Other sources, all optional:

- **Active task.** Five small files in `~/.claude/` written by the author's Claude Code setup:
  `current-task`, `current-task-name`, `current-task-status`, `current-task-step` and
  `current-task-due`. If they do not exist there is simply no active task line. Replace
  `jarvisd/collectors/task.py` to read another tracker.
- **Git repositories.** Only the ones you list under `[digest].repos` in `jarvis.local.toml`.
- **GitHub.** `[digest.github]`, through the `gh` command line, for the same repositories.
- **JARVIS's own logs.** Always on.

## What is written

One file per day: `<vault>/raw/jarvis/digest-YYYY-MM-DD.md` (the folder is `[paths].vault_write_raw`).
One file per week: `<vault>/raw/jarvis/weekly-YYYY-Www.md`, the weekly review (runs and cost,
decisions, dropped threads, Done and Snooze clicks, corrections by id), written by the first digest
of a new ISO week when the week before had something to review, or by `jarvis weekly`. The vault
writer refuses every other place. With `write_session_note = true` it also writes
`sessions/jarvis-*.md`; that is off by default. What the digest remembers between mornings (which
line it showed, what you marked done or snoozed) lives outside the vault, under `state/`.

## What a fresh install shows

On a machine with no vault, `jarvis run-digest --dry-run` prints `Items gated: 0` and the source
status `brain: brain_root_missing`, and `jarvis run-digest` writes an empty digest (state
"partial") that lists the failed source. A `--claude` run on such a vault makes no paid call
(`Claude: no_items`). That is the correct answer for an empty input, not a broken install. To see a real digest:

1. Create the folder structure above, or point `[paths]` at an existing notes folder.
2. Put a `RECENT.md` in it with one `## Open Threads` heading and a couple of
   `- [YYYY-MM-DD] ...` bullets dated within the last few days.
3. Run `jarvis run-digest --dry-run`. `Items gated` should now be above zero, and the printed
   payload is the exact text that would go to Claude.

If your notes do not look like this, the digest will mostly say "nothing found". Expect to adapt
`jarvisd/collectors/brain.py`: it is a single module of about 350 lines.
