<#
Hostile-daemon simulation for the JARVIS kill switch (report section 9c:
"tested against a hostile-daemon simulation before the daemon is trusted").

What this proves: the kill switch finds daemon-shaped processes, terminates all of them, stops
and disables the daemon's scheduled task, creates state/KILL, and records an audit line for each
step. It covers two shapes:

  A  the account branch of the later phases: processes selected by owner and command line marker.
  B  the v1 daemon started by Task Scheduler (design decision D1): a venv pythonw launcher with a
     python child, a claude-like grandchild, a heartbeat file, and a daemon that ignores state/KILL.
  C  the same daemon with no task behind it: found through the heartbeat pid and its tree.
  D  a stale heartbeat naming an unrelated process (pid reuse): the daemon is found by its command
     line marker, an orphaned claude by its argv, and the unrelated process is left alone.

What it does NOT prove: egress filtering, which is srt's job and is verified separately in phase 0
with elevation.

Safety design. The stand-ins carry unique markers in their command lines, live in a temp folder
and run under the CURRENT account. Every kill switch run gets its own task name, state folder, audit
file and markers (-TaskName, -StateDir, -AuditPath, -V1Marker, -ClaudeMarker), so it cannot match
the live JarvisDaemon task, its processes, its state/KILL or logs/killswitch.jsonl. The target
account is a name that does not exist, so the jarvis branch and the egress rule are never reached.
This script never runs Full scope, so it cannot drop your remote access, and it refuses to run
if the stand-in task name is the live one. It records the live daemon's state before and after and
fails if anything about it changed.
#>
[CmdletBinding()]
param(
  [int] $Count = 3,
  [int] $TimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'
$root       = Split-Path -Parent $PSScriptRoot
$killSwitch = Join-Path $root 'bin\kill-switch.ps1'
$liveTask   = 'JarvisDaemon'
$id         = [guid]::NewGuid().ToString('N').Substring(0, 8)
$work       = Join-Path $env:TEMP ("jarvis-hostile-sim-" + $id)
$auditPath  = Join-Path $work 'killswitch.jsonl'
$nobody     = 'hostile-sim-nobody'
$results    = [System.Collections.ArrayList]::new()

function Check([string]$name, [bool]$ok, [string]$detail = '') {
  $null = $results.Add([pscustomobject]@{ check = $name; pass = $ok; detail = $detail })
  $tag = if ($ok) { 'PASS' } else { 'FAIL' }
  Write-Host ("[{0}] {1} {2}" -f $tag, $name, $detail)
}

# The single place the kill switch is invoked. Every call carries all five overrides, so no run can
# fall back to the live task, state folder, audit file or markers.
function Invoke-KillSwitch([bool]$Dry, [string]$Reason, [string]$Account, [string]$Task, [string]$State,
                           [string]$V1, [string]$Claude, [string]$CmdMarker = '') {
  if ($Task -eq $liveTask -or $State -eq (Join-Path $root 'state')) { throw 'refusing to simulate against the live task or state' }
  $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $killSwitch, '-Scope', 'Daemon',
            '-Reason', $Reason, '-TargetAccount', $Account, '-TaskName', $Task, '-StateDir', $State,
            '-AuditPath', $auditPath, '-V1Marker', $V1, '-ClaudeMarker', $Claude)
  if ($CmdMarker) { $argv += @('-CommandLineMarker', $CmdMarker) }
  if ($Dry) { $argv += '-DryRun' }
  & powershell.exe @argv | Out-Host
}

function Get-SimProcs([string]$needle) {
  @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains($needle) -and $_.ProcessId -ne $PID -and
    $_.Name -match '^(pythonw?|powershell|pwsh)\.exe$' })
}

function Wait-Until([scriptblock]$cond, [int]$seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  do {
    if (& $cond) { return $true }
    Start-Sleep -Milliseconds 400
  } while ((Get-Date) -lt $deadline)
  return [bool](& $cond)
}

function Get-LastAudit {
  if (-not (Test-Path $auditPath)) { return $null }
  Get-Content $auditPath | Select-Object -Last 1 | ConvertFrom-Json
}
function Get-AuditLines { if (Test-Path $auditPath) { @(Get-Content $auditPath).Count } else { 0 } }
function Get-Steps($entry, [string]$step) { @($entry.actions | Where-Object { $_.step -eq $step }) }

# What the live daemon looks like from outside, read only. Compared before and after the run.
function Get-LiveFingerprint {
  $t = Get-ScheduledTask -TaskName $liveTask -ErrorAction SilentlyContinue
  $hb = Join-Path $root 'state\heartbeat.json'
  $hbPid = $null
  if (Test-Path $hb) { try { $hbPid = [int]((Get-Content $hb -Raw | ConvertFrom-Json).pid) } catch { $hbPid = $null } }
  $liveLog = Join-Path $root 'logs\killswitch.jsonl'
  [pscustomobject]@{
    registered = [bool]$t
    disabled   = [bool]($t -and $t.State -eq 'Disabled')
    kill       = (Test-Path (Join-Path $root 'state\KILL'))
    alive      = [bool]($hbPid -and (Get-Process -Id $hbPid -ErrorAction SilentlyContinue))
    ksLines    = if (Test-Path $liveLog) { @(Get-Content $liveLog).Count } else { 0 }
  }
}

if (-not (Test-Path $killSwitch)) { throw "kill switch not found at $killSwitch" }

$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
  $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
  if (-not $cmd) { throw 'no pythonw.exe found: create the venv with deploy\setup-venv.ps1 first' }
  $pythonw = $cmd.Source
}

New-Item -ItemType Directory -Force -Path $work | Out-Null
$fake = Join-Path $work 'fake_daemon.py'
# The stand-in daemon is hostile on purpose: it never looks at state/KILL, so only the kill switch
# can end it. 'daemon' mode beats a heartbeat and starts a claude-like child; 'sleep' just waits.
@'
import json, os, subprocess, sys, time
mode, tag, state = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else '')
if mode == 'sleep':
    time.sleep(3600)
    sys.exit(0)
child_tag = sys.argv[4] if len(sys.argv) > 4 else 'none'
subprocess.Popen([sys.executable, __file__, 'sleep', child_tag])
hb, tmp = os.path.join(state, 'heartbeat.json'), os.path.join(state, 'heartbeat.json.tmp')
os.makedirs(state, exist_ok=True)
while True:
    with open(tmp, 'w') as f:
        json.dump({'ts': time.strftime('%Y-%m-%dT%H:%M:%S+00:00'), 'pid': os.getpid(), 'job_id': None,
                   'version': 'sim', 'mode': 'task'}, f)
    os.replace(tmp, hb)
    time.sleep(1)
'@ | Set-Content -Path $fake -Encoding ascii

function Start-Fake([string]$mode, [string]$tag, [string]$state = '', [string]$childTag = 'none') {
  $a = '"{0}" {1} {2} "{3}" {4}' -f $fake, $mode, $tag, $state, $childTag
  Start-Process -FilePath $pythonw -ArgumentList $a -WindowStyle Hidden -PassThru
}

$live0 = Get-LiveFingerprint
$mA = "HSIM-A-$id"; $mB = "HSIM-B-$id"; $mC = "HSIM-C-$id"; $mD = "HSIM-D-$id"
$cB = "CLD-B-$id"; $cC = "CLD-C-$id"; $cD = "CLD-D-$id"
$bystanderTag = "BYSTANDER-$id"
$simTask = "JarvisDaemonSim-$id"
$stateB = Join-Path $work 'state-B'; $stateC = Join-Path $work 'state-C'; $stateD = Join-Path $work 'state-D'
$stateA = Join-Path $work 'state-A'
Write-Host "hostile-sim id: $id (work folder $work)"

try {
  # Bystanders live through every scenario. They are python and powershell processes under the
  # same account, like the owner's own programs, and carry none of the markers.
  $bys = Start-Fake 'sleep' $bystanderTag
  $bysPs = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru -ArgumentList @(
    '-NoProfile', '-Command', "Start-Sleep -Seconds 3600 # $bystanderTag-ps")
  Start-Sleep -Seconds 2
  Check 'bystanders-started' ((Get-SimProcs $bystanderTag).Count -ge 2) ("found " + (Get-SimProcs $bystanderTag).Count)

  # --- A. account branch, marker mode -----------------------------------------------------------------
  Write-Host "`n=== A. account branch (owner and marker) ==="
  for ($i = 1; $i -le $Count; $i++) {
    $inner = @"
`$scratch = Join-Path `$env:TEMP '$mA-$i.log'
while (`$true) {
  "$mA tick `$(Get-Date -Format o) simulated-egress-attempt simulated-write-outside-vault" |
    Add-Content -Path `$scratch -Encoding utf8
  Start-Sleep -Seconds 2
}
"@
    Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-WindowStyle', 'Hidden', '-Command', $inner -WindowStyle Hidden | Out-Null
  }
  Start-Sleep -Seconds 3
  $aliveA = @(Get-SimProcs $mA)
  Check 'A-processes-spawned' ($aliveA.Count -eq $Count) ("expected $Count, found " + $aliveA.Count)
  if ($aliveA.Count -eq 0) { throw 'simulation could not start: nothing to kill, aborting' }

  $n0 = Get-AuditLines
  Invoke-KillSwitch $true 'hostile-sim-A-dry' $env:USERNAME $simTask $stateA "NOPE-V1-A-$id" "NOPE-CLD-A-$id" $mA
  Check 'A-dryrun-kills-nothing' (@(Get-SimProcs $mA).Count -eq $aliveA.Count) ("still alive: " + @(Get-SimProcs $mA).Count)
  Invoke-KillSwitch $false 'hostile-sim-A' $env:USERNAME $simTask $stateA "NOPE-V1-A-$id" "NOPE-CLD-A-$id" $mA
  $goneA = Wait-Until { @(Get-SimProcs $mA).Count -eq 0 } $TimeoutSeconds
  Check 'A-all-processes-terminated' $goneA ("survivors: " + @(Get-SimProcs $mA).Count)
  $last = Get-LastAudit
  Check 'A-audit-lines-appended' ((Get-AuditLines) -eq $n0 + 2) ("lines $n0 -> " + (Get-AuditLines))
  Check 'A-audit-records-marker' ($last.marker -eq $mA) ("marker=" + $last.marker)
  Check 'A-audit-not-dryrun' ($last.dry_run -eq $false -and $last.scope -eq 'Daemon') ("dry_run=" + $last.dry_run)

  # --- B. the v1 daemon under Task Scheduler ----------------------------------------------------------
  Write-Host "`n=== B. v1 daemon started by Task Scheduler ==="
  $act = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}" daemon {1} "{2}" {3}' -f $fake, $mB, $stateB, $cB)
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
  $prin = New-ScheduledTaskPrincipal -UserId ($env:USERDOMAIN + '\' + $env:USERNAME) -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $simTask -Action $act -Settings $set -Principal $prin -Description 'JARVIS hostile simulation stand-in, removed by the simulation' | Out-Null
  Start-ScheduledTask -TaskName $simTask
  $hbB = Join-Path $stateB 'heartbeat.json'
  $upB = Wait-Until { (Test-Path $hbB) -and (@(Get-SimProcs $cB).Count -ge 1) } $TimeoutSeconds
  $procsB = @(Get-SimProcs $mB) + @(Get-SimProcs $cB)
  Check 'B-daemon-tree-running' ($upB -and $procsB.Count -ge 3) ("processes: " + $procsB.Count + " (launcher, daemon, claude-like)")
  Check 'B-task-registered-and-not-disabled' ((Get-ScheduledTask -TaskName $simTask).State -ne 'Disabled') ("state=" + (Get-ScheduledTask -TaskName $simTask).State)

  Invoke-KillSwitch $true 'hostile-sim-B-dry' $nobody $simTask $stateB $mB $cB
  $dry = Get-LastAudit
  $wouldKill = @(Get-Steps $dry 'kill-daemon-tree' | Where-Object { $_.result -eq 'would-kill' }).Count
  Check 'B-dryrun-reports-the-tree' ($wouldKill -ge 3) ("would-kill lines: $wouldKill")
  Check 'B-dryrun-changes-nothing' (
    (@(Get-SimProcs $mB).Count + @(Get-SimProcs $cB).Count -eq $procsB.Count) -and
    ((Get-ScheduledTask -TaskName $simTask).State -ne 'Disabled') -and
    (-not (Test-Path (Join-Path $stateB 'KILL')))) 'processes, task and state/KILL unchanged'
  Check 'B-dryrun-audit' ($dry.dry_run -eq $true -and (Get-Steps $dry 'create-kill-file')[0].result -eq 'would-create')

  Invoke-KillSwitch $false 'hostile-sim-B' $nobody $simTask $stateB $mB $cB
  $goneB = Wait-Until { (@(Get-SimProcs $mB).Count + @(Get-SimProcs $cB).Count) -eq 0 } $TimeoutSeconds
  Check 'B-all-processes-terminated' $goneB ("survivors: " + (@(Get-SimProcs $mB).Count + @(Get-SimProcs $cB).Count))
  Check 'B-task-disabled' ((Get-ScheduledTask -TaskName $simTask).State -eq 'Disabled') ("state=" + (Get-ScheduledTask -TaskName $simTask).State)
  Check 'B-kill-file-created' (Test-Path (Join-Path $stateB 'KILL')) 'state/KILL exists in the stand-in state folder'
  $real = Get-LastAudit
  Check 'B-audit-each-step' (
    ((Get-Steps $real 'create-kill-file')[0].result -eq 'ok') -and ((Get-Steps $real 'stop-task')[0].result -eq 'ok') -and
    ((Get-Steps $real 'daemon-targets')[0].result -eq 'found') -and (@(Get-Steps $real 'kill-daemon-tree').Count -ge 3)) (
    "steps: " + (($real.actions | ForEach-Object { $_.step + '=' + $_.result }) -join ' '))
  Check 'B-audit-not-dryrun' ($real.dry_run -eq $false -and $real.target -eq $nobody) ("dry_run=" + $real.dry_run + " target=" + $real.target)
  Unregister-ScheduledTask -TaskName $simTask -Confirm:$false -ErrorAction SilentlyContinue

  # --- C. no task: found through the heartbeat pid and the process tree ------------------------------------------
  Write-Host "`n=== C. daemon without a task (heartbeat pid and tree) ==="
  $dC = Start-Fake 'daemon' $mC $stateC $cC
  $hbC = Join-Path $stateC 'heartbeat.json'
  $upC = Wait-Until { (Test-Path $hbC) -and (@(Get-SimProcs $cC).Count -ge 1) } $TimeoutSeconds
  $procsC = @(Get-SimProcs $mC) + @(Get-SimProcs $cC)
  Check 'C-daemon-tree-running' ($upC -and $procsC.Count -ge 3) ("processes: " + $procsC.Count)
  # The claude marker is deliberately wrong here, so the claude-like child can only die as a
  # descendant of the daemon, not as an orphan match.
  Invoke-KillSwitch $false 'hostile-sim-C' $nobody $simTask $stateC $mC "NOPE-CLD-C-$id"
  $goneC = Wait-Until { (@(Get-SimProcs $mC).Count + @(Get-SimProcs $cC).Count) -eq 0 } $TimeoutSeconds
  Check 'C-all-processes-terminated' $goneC ("survivors: " + (@(Get-SimProcs $mC).Count + @(Get-SimProcs $cC).Count))
  $realC = Get-LastAudit
  $roles = @(Get-Steps $realC 'kill-daemon-tree' | ForEach-Object { $_.detail })
  Check 'C-found-by-heartbeat-and-tree' (
    (@($roles | Where-Object { $_ -match 'role=daemon' }).Count -eq 1) -and
    (@($roles | Where-Object { $_ -match 'role=child' }).Count -ge 1)) ("roles: " + ($roles -join ' | '))
  Check 'C-kill-file-created' (Test-Path (Join-Path $stateC 'KILL')) 'state/KILL exists'

  # --- D. stale heartbeat naming an unrelated process; orphaned claude ------------------------------------------
  Write-Host "`n=== D. stale heartbeat pid (reuse) and an orphaned claude ==="
  New-Item -ItemType Directory -Force -Path $stateD | Out-Null
  $dD = Start-Fake 'sleep' $mD
  $oD = Start-Fake 'sleep' $cD
  Start-Sleep -Seconds 2
  # The heartbeat names the unrelated bystander, as a recycled pid would.
  (@{ ts = '2026-01-01T00:00:00+00:00'; pid = $bys.Id; mode = 'task' } | ConvertTo-Json -Compress) |
    Set-Content -Path (Join-Path $stateD 'heartbeat.json') -Encoding ascii
  Check 'D-daemon-like-and-orphan-running' ((@(Get-SimProcs $mD).Count -ge 1) -and (@(Get-SimProcs $cD).Count -ge 1)) 'both present'
  Invoke-KillSwitch $false 'hostile-sim-D' $nobody $simTask $stateD $mD $cD
  $goneD = Wait-Until { (@(Get-SimProcs $mD).Count + @(Get-SimProcs $cD).Count) -eq 0 } $TimeoutSeconds
  Check 'D-marker-and-orphan-terminated' $goneD ("survivors: " + (@(Get-SimProcs $mD).Count + @(Get-SimProcs $cD).Count))
  $realD = Get-LastAudit
  $rolesD = @(Get-Steps $realD 'kill-daemon-tree' | ForEach-Object { $_.detail })
  Check 'D-roles-marker-and-claude-orphan' (
    (@($rolesD | Where-Object { $_ -match 'role=marker' }).Count -ge 1) -and
    (@($rolesD | Where-Object { $_ -match 'role=claude-orphan' }).Count -ge 1)) ("roles: " + ($rolesD -join ' | '))
  Check 'D-stale-heartbeat-pid-refused' ((Get-Steps $realD 'daemon-targets')[0].detail -match 'not a jarvisd') ((Get-Steps $realD 'daemon-targets')[0].detail)

  # --- the bystanders and the live daemon ----------------------------------------------------------------------
  Write-Host "`n=== bystanders and the live daemon ==="
  Check 'bystanders-survived-every-scenario' (
    [bool](Get-Process -Id $bys.Id -ErrorAction SilentlyContinue) -and [bool](Get-Process -Id $bysPs.Id -ErrorAction SilentlyContinue)) 'unmarked python and powershell still running'
  $live1 = Get-LiveFingerprint
  Check 'live-daemon-untouched' (
    ($live1.registered -eq $live0.registered) -and ($live1.disabled -eq $live0.disabled) -and ($live1.kill -eq $live0.kill) -and
    ($live1.alive -eq $live0.alive) -and ($live1.ksLines -eq $live0.ksLines)) (
    "task registered=$($live1.registered) disabled=$($live1.disabled), state/KILL=$($live1.kill), heartbeat pid alive=$($live1.alive), live audit lines=$($live1.ksLines)")
}
finally {
  # Whatever happened above, leave nothing running and nothing registered.
  Unregister-ScheduledTask -TaskName $simTask -Confirm:$false -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine.Contains($work) -or $_.CommandLine.Contains("-$id")) -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 800
  Get-ChildItem -Path $env:TEMP -Filter "HSIM-A-$id-*.log" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force -Path $work -ErrorAction SilentlyContinue
}

$failed = @($results | Where-Object { -not $_.pass }).Count
Write-Host ("`nhostile-sim: {0}/{1} checks passed" -f ($results.Count - $failed), $results.Count)
if ($failed -gt 0) {
  Write-Host 'RESULT: FAIL'
  exit 1
}
Write-Host 'RESULT: PASS (Daemon scope only; Full scope must be tested at the machine)'
exit 0
