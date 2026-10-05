# First live run, 2026-10-05

The author's log of the first real run of the v1 digest daemon on the machine it was built for.
It is kept as evidence of what was observed (not only what the tests assert) and of the defects
the first real note exposed. The operator manual is `docs/v1-operations.md`; the design is
`docs/v1-design.md`.

Setup: Windows 11, Claude Code 2.1.289, Python 3.12 in `.venv`, a notes vault under `~/brain`.

1. `jarvis.local.toml` written: 19 repositories (a mix of work and personal, a few set to
   `counts_only`), 9 sensitive terms, 2 extra sensitive path globs and a private deny list for the
   hygiene test. The work-repository flag stayed `false`, so work metadata never reached Claude.
2. `jarvis self-test`: 12 of 12 PASS, 1 SKIP (the scheduled task was not registered yet).
3. `jarvis run-digest --dry-run`: 9 items gated, 4 to Claude, 6 held (1 sensitive term hit on
   `RECENT.md` as a whole, 5 policy holds). The payload was 355 bytes and was read before the
   first paid call.
4. `jarvis run-digest --claude`: note written, 1 Claude call, 0.0068 USD, 961 cache-creation input
   tokens, 295 output tokens. Toast adapter reported ok. `jarvis audit verify`: chain ok, head
   seq 40.
5. Findings from the first note: `RECENT.md` was withheld whole on one term hit (fixed the same
   day: the term scan now works per bullet), and the "what JARVIS did while you slept" block said
   the kill-switch and watchdog logs were unavailable although they existed (fixed: the system
   collector parses them).
6. The daily trigger of the scheduled task was written as 06:00 UTC instead of 06:00 local. Fixed
   in `deploy/register-jarvisd-task.ps1`.
7. `jarvis install-task --apply`: task `JarvisDaemon` registered (AtLogOn plus Daily, Interactive,
   Limited, StartWhenAvailable, no WakeToRun). `schtasks /run /tn JarvisDaemon`: a heartbeat
   appeared 7 s later in task mode.
8. `bin/kill-switch.ps1 -Scope Daemon -DryRun`: reported the stop-task step, no process match for
   the `jarvis` account (expected until `docs/killswitch-v1-patch.md` is applied; it was applied
   later, see "Kill switch for the v1 daemon" in `docs/v1-operations.md`) and that the egress
   step needs elevation. A real trip was not exercised that day.
9. Dry run after the fixes: 27 items gated, 22 to Claude, 7 held. The daemon was restarted so the
   task ran the corrected code.

Still open after that day: the unattended 06:30 run of the next morning, one `jarvis wrong`
exercise, a real kill-switch trip, and the decision whether work-repository metadata may reach
Claude.
