---
type: jarvis-digest
generator: jarvisd
generator_version: 1.2.0
job_id: digest-2026-10-06
date: 2026-10-06
generated_at: 2026-10-06T06:31:40+01:00
window_start: 2026-10-05T05:30:12+01:00
window_end: 2026-10-06T06:30:30+01:00
status: complete
late: false
claude: no_items
local_tier: not_installed
degraded: false
cost_usd: 0.0000
items: {collected: 10, cleared: 0, held_sensitive: 10, held_policy: 0, over_cap: 0}
grammar: 2
n_collected: 10
n_cleared: 0
n_held: 10
n_start_here: 0
n_attention: 0
n_still_open: 0
n_still_open_hidden: 0
n_decided: 0
n_repos_active: 0
n_repos_quiet: 1
n_system_anomalies: 0
n_since_new: 0
n_since_resolved: 0
n_since_dropped: 0
n_since_returned: 0
sources: {brain: ok, task: ok, git: ok, system: ok, clickup: disabled, github: not_collected}
audit_seq: 812
audit_head: abababababababababababababababababababababababababababababababab
tags: [jarvis, digest]
---
# JARVIS morning digest, Tuesday 2026-10-06

## Start here
Claude summary unavailable (no items). Deterministic sections below are complete.

## Attention
- Nothing broken.

## Active task
- Active task withheld, see Held back and not summarized.

## Repos
- Quiet: 1 repo.
- Not a git repo or missing: example-missing.
- GitHub PRs and CI: not collected (the github collector did not run).

## System
- All green: 1 job done, 0 failed, $0.04 Claude, breaker closed, disk ok, tasks ok.
- Checkpoints waiting for /promote-sessions: 2.

## Held back and not summarized
- Held: 10 sensitive (ids 0a1b2c3d, 4d4d4d4d, 5a5a5a5a, 6b6b6b6b, 9f8e7d6c, a1b2c3d4, b2c3d4e5, c0ffee01, c3d4e5f6, e5f6a7b8; reasons: term:0 x10), 0 policy, 0 over cap. Claude: not called. Run `jarvis held` in a terminal.

## Source status
- brain ok (6 items), task ok, git ok (4 repos, 1 not repo), system ok, clickup disabled, github not collected.

## Flag a mistake
- `jarvis wrong <id> --should {escalate,hold,skip,other} --note "..."`; `--leak` if something sensitive was shown or sent.
