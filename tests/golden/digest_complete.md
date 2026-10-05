---
type: jarvis-digest
generator: jarvisd
generator_version: 1.1.0
job_id: digest-2026-10-06
date: 2026-10-06
generated_at: 2026-10-06T06:31:40+01:00
window_start: 2026-10-05T05:30:12+01:00
window_end: 2026-10-06T06:30:30+01:00
status: complete
late: false
claude: ok
local_tier: not_installed
degraded: false
cost_usd: 0.0412
items: {collected: 10, cleared: 7, held_sensitive: 2, held_policy: 1, over_cap: 0}
sources: {brain: ok, task: ok, git: ok, system: ok, clickup: disabled, github: not_collected}
audit_seq: 812
audit_head: abababababababababababababababababababababababababababababababab
tags: [jarvis, digest]
---
# JARVIS morning digest, Tuesday 2026-10-06

## Start here
One task is overdue and one repo moved overnight.
1. [c0ffee01] Review task is overdue since yesterday
2. [b2c3d4e5] One commit landed in the notes repo

## Active task
- 123synth fix(parser): synthetic task name, status IN REVIEW, due 2026-10-05 (OVERDUE). No ClickUp call was made (v1).

## Brain: open threads and decisions
- [2026-10-04] Synthetic thread waiting on a reviewer [e5f6a7b8] Waiting on the reviewer, nudge today
- [2026-09-20] (stale) Synthetic stale thread [0a1b2c3d]
- Decision [2026-10-03] Synthetic decision, because it is a fixture [9f8e7d6c] Fixture decision, nothing to do
- Session 2026-10-05-20, entry point: Continue at fixture.py:10, wire the synthetic thing [5a5a5a5a]
- Session 2026-10-05-20, open thread: Synthetic follow up one [6b6b6b6b]

## Repos
- example-api (work) branch test, 3 commits since window, 2 modified, 1 untracked: fix: synthetic parser bug; feat: synthetic endpoint [a1b2c3d4]
- example-notes: 1 commit since window, 0 modified, 0 untracked [b2c3d4e5]
- Quiet: example-web.
- Not a git repo or missing: example-missing.
- GitHub PRs and CI: not collected (the github collector did not run).

## What JARVIS did while you slept
- Jobs: 1 done, 0 failed. Claude calls: 1 ($0.04, 3.2k tokens). Vault writes: 1. Breaker: closed.
- Daemon starts since last digest: 2. Unclean exits: 0.
- ExampleNightly: last run 2026-10-06 02:30, result 0.
- Kill switch trips: 0. Watchdog crashloops: 0.
- RECENT.md age 4.1 h.
- Checkpoints waiting for /promote-sessions: 2.

## Held back and not summarized
- Sensitive, never read or sent: 2 items (ids w-3a9f1c, w-77d20e; reasons: component_sensitive x1, tag_frontmatter x1). Run `jarvis held` in a terminal.
- Policy (work metadata to Claude disabled): 1 item, rendered above without summary.
- Over size cap: 0 items. Claude unavailable: none.

## Source status
- brain ok (5 items), task ok, git ok (4 repos, 1 not repo), system ok, clickup disabled, github not collected.

## Flag a mistake
- `jarvis wrong <id> --should {escalate,hold,skip,other} --note "..."`; `--leak` if something sensitive was shown or sent.
