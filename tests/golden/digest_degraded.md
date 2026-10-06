---
type: jarvis-digest
generator: jarvisd
generator_version: 1.2.0
job_id: digest-2026-10-06
date: 2026-10-06
generated_at: 2026-10-06T06:31:40+01:00
window_start: 2026-10-05T05:30:12+01:00
window_end: 2026-10-06T06:30:30+01:00
status: degraded_no_llm
late: true
claude: budget
local_tier: not_installed
degraded: true
cost_usd: 0.0000
items: {collected: 8, cleared: 8, held_sensitive: 0, held_policy: 0, over_cap: 0}
sources: {brain: ok, task: ok, git: ok, system: ok, clickup: disabled, github: not_collected}
audit_seq: 812
audit_head: abababababababababababababababababababababababababababababababab
tags: [jarvis, digest]
---
# JARVIS morning digest, Tuesday 2026-10-06

## Start here
Claude summary unavailable (budget). Deterministic sections below are complete.
1. [c0ffee01] fix(parser): synthetic task name is overdue
2. [a1b2c3d4] example-api: 3 commits since the window start
3. [b2c3d4e5] example-notes: 1 commit since the window start
4. [e5f6a7b8] Synthetic thread waiting on a reviewer
5. [0a1b2c3d] Synthetic stale thread

## Active task
- 123synth fix(parser): synthetic task name, status IN REVIEW, due 2026-10-05 (OVERDUE). No ClickUp call was made (v1).

## Brain: open threads and decisions
- [2026-10-04] Synthetic thread waiting on a reviewer [e5f6a7b8]
- [2026-09-20] (stale) Synthetic stale thread [0a1b2c3d]
- Decision [2026-10-03] Synthetic decision, because it is a fixture [9f8e7d6c]
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
- Sensitive, never read or sent: 0 items.
- Policy (work metadata to Claude disabled): 0 items.
- Over size cap: 0 items. Claude unavailable: budget.

## Source status
- brain ok (5 items), task ok, git ok (4 repos, 1 not repo), system ok, clickup disabled, github not collected.

## Flag a mistake
- `jarvis wrong <id> --should {escalate,hold,skip,other} --note "..."`; `--leak` if something sensitive was shown or sent.
