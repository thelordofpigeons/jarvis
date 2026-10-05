# Kill switch patch for the v1 daemon

Status: applied (owner-approved 2026-10-05) to `bin/kill-switch.ps1`, with the changes below, and
verified by `tests/hostile-sim.ps1` (27 of 27 checks, results in `docs/v1-operations.md`). The
diff further down is the original proposal and is kept for the record.

Spec references are to `docs/v1-design.md`: section 1 (decision D1), section 10 (kill, pause,
crash) and section 16 (known gaps).

## Why the patch was needed

Design decision D1 runs `jarvisd` as the owner's own account, not as the `jarvis` account. That
made two parts of the Phase 0 `bin/kill-switch.ps1` blind to it:

1. The process-kill step selects processes whose owner equals `$TargetAccount`, which
   defaults to `jarvis`. A daemon owned by `<owner>` was never matched.
2. The egress block rule `JARVIS-daemon-egress-block` is keyed to the `jarvis` SID. Enabling
   it does nothing to an `<owner>` process, and it must never be widened to that
   account because that would cut the owner's own network.

The task-level kill (stop and disable the scheduled task `JarvisDaemon`) already worked without
any edit. The process-level kill and the egress step did not cover an `<owner>` daemon. That was
the gap listed in section 16.

## What is applied

The Daemon scope now does these steps, in this order, and writes one audit action for each:

| Step | Audit step name | What it does |
|---|---|---|
| lock probe | `daemon-lock` | Tries to take the byte that the daemon holds in `state/daemon.lock`. `held` proves a live daemon, `free` or `absent` does not. |
| find the tree | `daemon-targets` | Reads the pid in `state/heartbeat.json`, checks that the process behind it is a `jarvisd` (a pid can be reused), adds its launcher parent (the venv `pythonw.exe`) and every descendant, so the `claude` child is included. Also adds any process of the owner that carries the command line marker `jarvisd serve --task`, and any orphaned `claude` carrying the isolated argv `--no-session-persistence --permission-prompts none`. |
| kill file | `create-kill-file` | Writes `state/KILL`. The daemon exits 3 on it and refuses to start again until a human removes it, so a task restart cannot bring it back. |
| task | `stop-task` | Stops and disables `JarvisDaemon`. |
| kill | `kill-daemon-tree` | Stops each process found, daemon and launcher first. `gone` means Task Scheduler's own teardown from the step above got there first. |
| account branch | `kill-processes` | Kept for the later phases that move the daemon to the `jarvis` account: owner filter, by image name when no marker is given. |
| egress | `revoke-egress` | Enables the SID-keyed rule for the `jarvis` target, reports `not-applicable` for any other target. |

Selection never works by image name for the human account: only the heartbeat pid, its tree and
the two command line markers are used, and a marker match also needs an image name in
`claude, node, python, pythonw, jarvisd` and the owner to be the current account. The script's own
process, every ancestor and any process running `kill-switch.ps1` are excluded, which is the bug
the hostile simulation originally caught. A dev daemon started by hand without `--task` is found
through its heartbeat when it has one, runs with Claude disabled (section 11), and exits on
`state/KILL` like the real one.

## How this differs from the original proposal

- The v1 steps do not depend on `-TargetAccount`. The proposal only reached the v1 daemon when
  the `JarvisKillSwitch` task was retargeted with `-TargetAccount <owner>` and a marker. Here the
  task registered by Phase 0 (`-Scope Daemon -Reason watchdog-trip`, default target `jarvis`)
  already finds and kills the v1 daemon, so the watchdog's `schtasks /run /tn JarvisKillSwitch`
  works without re-registering anything and the `jarvis` branch stays intact for later phases.
  The proposal's defaulting rule is still there: a non-`jarvis` target with no marker gets
  `jarvisd serve --task` as its marker, and egress reports `not-applicable`.
- The `bin/phase0-elevated.ps1` hunk is not applied, for the same reason. If you want both
  targets reported explicitly, register a second task with the arguments from the proposal.
- New parameters: `-TaskName`, `-StateDir`, `-AuditPath`, `-V1Marker`, `-ClaudeMarker`. Their
  defaults are the real daemon. They exist so the hostile simulation can run the real script
  against a stand-in without touching the live task, `state/` or `logs/killswitch.jsonl`.
- `state/KILL` is created by the kill switch, which the proposal did not do. The audit gains the
  steps `daemon-lock`, `daemon-targets`, `create-kill-file` and `kill-daemon-tree`.

The `jarvis self-test` check `killswitch-alignment` reads the script and compares its task name
and marker with the daemon's own, so a rename on one side fails the self-test.

## How to verify

1. Dry run, with the daemon running as the task, from any shell:
   `powershell -NoProfile -ExecutionPolicy Bypass -File bin\kill-switch.ps1 -DryRun -AuditPath $env:TEMP\ks-dry.jsonl`.
   Expect `daemon-targets` to list the launcher and the daemon, `create-kill-file` and
   `stop-task` as would-do lines, and one `would-kill` per process. Without `-AuditPath` the dry
   run appends a line to the live `logs/killswitch.jsonl`, which the digest counts.
2. The hostile simulation, which never touches the live task:
   `powershell -NoProfile -ExecutionPolicy Bypass -File tests\hostile-sim.ps1`.
   It runs four scenarios: the account branch, a daemon under a stand-in scheduled task (launcher,
   child, claude-like grandchild, a daemon that ignores `state/KILL`), the same without a task
   (heartbeat pid and tree), and a stale heartbeat naming an unrelated process plus an orphaned
   claude. Bystander processes must survive and the live daemon must be unchanged. Under pytest it
   is opt-in: `JARVIS_RUN_HOSTILE_SIM=1`.
3. The real path, once, at the machine and only when you mean it: `schtasks /run /tn JarvisKillSwitch`
   while `JarvisDaemon` runs. Then check `logs/killswitch.jsonl`, run `jarvis status`, and recover
   with `Remove-Item state\KILL` and `Enable-ScheduledTask -TaskName JarvisDaemon`. This has not
   been done on the live daemon yet.
4. `python bin\watchdog.py --self-test` must still report all checks passing.

## What stays true after the patch

The v1 daemon is still not sandboxed and still not isolated from the owner's files by an OS
boundary (section 16). The patch closes the process-kill gap only. The audit chain stays
tamper-evident, not tamper-proof. Not proven: the elevated path (the `jarvis` branch and the
firewall rule) against a v1 daemon, and the real scheduled task `JarvisKillSwitch` end to end
with the new script, because those need Administrator and a real trip.

## Original proposal (kept for the record)

The unified diff below was the proposal. The `bin/kill-switch.ps1` hunk is in the script in
extended form; the `bin/phase0-elevated.ps1` hunk is not applied (see above).

```diff
diff --git a/bin/kill-switch.ps1 b/bin/kill-switch.ps1
index 6dfd65f..d8a7d84 100644
--- a/bin/kill-switch.ps1
+++ b/bin/kill-switch.ps1
@@ -33,6 +33,13 @@ $ruleName  = 'JARVIS-daemon-egress-block'
 $daemonTask = 'JarvisDaemon'
 $daemonProcessNames = @('llama-server','llama-swap','jarvisd','python','pythonw')
 
+# v1 option 2 runs the daemon as the human account. Selecting by process name there would hit
+# every python and pythonw the owner has open, so any target other than 'jarvis' is matched by
+# command line marker only. The default marker is the daemon's own task command line.
+if ($TargetAccount -ne 'jarvis' -and -not $CommandLineMarker) {
+  $CommandLineMarker = 'jarvisd serve --task'
+}
+
 $isAdmin = ([Security.Principal.WindowsPrincipal] `
   [Security.Principal.WindowsIdentity]::GetCurrent()
 ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
@@ -115,7 +122,11 @@ try {
 }
 
 # 3. Revoke egress. Phase 0 pre-creates the rule disabled; this enables it.
-if ($isAdmin) {
+# The rule is keyed to the jarvis SID. For any other target it would constrain nothing, so it
+# is reported as not applicable instead of being enabled for no effect.
+if ($TargetAccount -ne 'jarvis') {
+  Note 'revoke-egress' 'not-applicable' "egress rule is keyed to the jarvis SID, not '$TargetAccount'"
+} elseif ($isAdmin) {
   try {
     $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction Stop
     if ($DryRun) {
diff --git a/bin/phase0-elevated.ps1 b/bin/phase0-elevated.ps1
index 10a87ed..88bc842 100644
--- a/bin/phase0-elevated.ps1
+++ b/bin/phase0-elevated.ps1
@@ -191,7 +191,7 @@ try {
   } else {
     $ks = Join-Path $root 'bin\kill-switch.ps1'
     $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
-      -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Scope Daemon -Reason watchdog-trip' -f $ks)
+      -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Scope Daemon -Reason watchdog-trip -TargetAccount <owner> -CommandLineMarker "jarvisd serve --task"' -f $ks)
     $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:COMPUTERNAME, $OwnerAccount) `
       -LogonType S4U -RunLevel Highest
     Register-ScheduledTask -TaskName $TASK -Action $action -Principal $principal `
```
