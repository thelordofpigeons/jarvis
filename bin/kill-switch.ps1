<#
JARVIS kill switch (report section 9c).

Runs under the HUMAN account, not the daemon account. Needs elevation for the
firewall, service and Tailscale actions, so phase 0 registers it as a Task
Scheduler task with RunLevel Highest. The non-elevated watchdog trips it with:
    schtasks /run /tn JarvisKillSwitch

Scopes:
  Daemon  stops and disables the daemon task, creates state/KILL, kills the v1 daemon's
          process tree and any claude child, kills processes of the jarvis account, and blocks
          that account's egress. Safe to run remotely. This is the default.
  Full    also stops RustDesk, drops sshd sessions and takes Tailscale down.
          This severs YOUR remote access. Only run Full while physically at the machine.

The v1 daemon (design decision D1, docs/killswitch-v1-patch.md) runs as the human account, so
it is found by what it IS, never by image name: the pid in state/heartbeat.json (verified to be a
jarvisd before anything is killed, because a pid can be reused), its launcher parent and every
descendant, any process carrying the daemon's command line marker, and any orphaned claude
carrying the isolated argv. The jarvis-account branch (owner filter, SID-keyed egress rule) is
kept for the later phases that move the daemon to that account.

Every run appends one JSON line to logs/killswitch.jsonl whether it succeeds or not. The
-TaskName, -StateDir and -AuditPath overrides exist so the hostile simulation can run the real
script against a stand-in without touching the live task, state or audit log.
#>
[CmdletBinding()]
param(
  [ValidateSet('Daemon','Full')] [string] $Scope = 'Daemon',
  [switch] $DryRun,
  [string] $Reason = 'manual',
  [string] $TriggeredBy = $env:USERNAME,
  [string] $TargetAccount = 'jarvis',
  # When set, ONLY processes whose command line contains this marker are killed.
  # The hostile simulation uses it so a test can never touch real processes.
  [string] $CommandLineMarker = '',
  # The v1 daemon's identity. Defaults are the real daemon; the simulation overrides all of them.
  [string] $TaskName = 'JarvisDaemon',
  [string] $StateDir = '',
  [string] $AuditPath = '',
  [string] $V1Marker = 'jarvisd serve --task',
  [string] $ClaudeMarker = '--no-session-persistence --permission-prompts none'
)

$ErrorActionPreference = 'Continue'
$root      = Split-Path -Parent $PSScriptRoot
if (-not $AuditPath) { $AuditPath = Join-Path $root 'logs\killswitch.jsonl' }
if (-not $StateDir)  { $StateDir  = Join-Path $root 'state' }
$ruleName  = 'JARVIS-daemon-egress-block'
$daemonTask = $TaskName
$daemonProcessNames = @('llama-server','llama-swap','jarvisd','python','pythonw')
# Image names the v1 selection may ever touch by marker. Descendants of a verified daemon are
# killed whatever their name, since they are its children.
$v1Names = @('claude','node','python','pythonw','jarvisd')

# v1 option 2 runs the daemon as the human account. Selecting by process name there would hit
# every python and pythonw the owner has open, so any target other than 'jarvis' is matched by
# command line marker only. The default marker is the daemon's own task command line.
if ($TargetAccount -ne 'jarvis' -and -not $CommandLineMarker) {
  $CommandLineMarker = $V1Marker
}

$isAdmin = ([Security.Principal.WindowsPrincipal] `
  [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$actions = [System.Collections.ArrayList]::new()
function Note([string]$step, [string]$result, [string]$detail = '') {
  $null = $actions.Add([ordered]@{ step = $step; result = $result; detail = $detail })
  $tag = if ($DryRun) { 'DRYRUN' } else { $result.ToUpper() }
  Write-Host ("[{0}] {1} {2}" -f $tag, $step, $detail)
}

Write-Host ("JARVIS kill switch: scope={0} dryrun={1} admin={2}" -f $Scope, [bool]$DryRun, $isAdmin)
if (-not $isAdmin) {
  Note 'elevation' 'missing' 'not elevated: firewall, service and tailscale steps will be skipped'
}

# Self-preservation: this script's own PID and every ancestor are excluded from every kill, as
# is any process running kill-switch.ps1. Without this the switch matches its own command line
# (the markers are arguments on it), kills itself mid-run, and never writes the audit line.
# The hostile simulation caught exactly that.
$selfChain = @()
$cur = $PID
while ($cur -and $cur -ne 0 -and ($selfChain -notcontains $cur)) {
  $selfChain += $cur
  $parent = try { (Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction Stop).ParentProcessId } catch { $null }
  $cur = $parent
}

# One snapshot of the process table drives target selection, so the dry run and the real run
# see the same thing and the tree walk is not racing a moving table.
$snapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$byId = @{}
foreach ($sp in $snapshot) { $byId[[int]$sp.ProcessId] = $sp }

function Test-IsSelf($p) {
  if ($selfChain -contains [int]$p.ProcessId) { return $true }
  return [bool]($p.CommandLine -and $p.CommandLine -match 'kill-switch\.ps1')
}
function Get-ProcOwner($p) {
  try { (Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction Stop).User } catch { $null }
}
function Get-BaseName($p) { [System.IO.Path]::GetFileNameWithoutExtension([string]$p.Name) }

# A heartbeat pid is only trusted when the process behind it is a jarvisd: a stale file can hold
# a pid that Windows has since given to an unrelated program.
function Test-DaemonIdentity($p) {
  if (-not $p -or -not $p.CommandLine) { return $false }
  if ($v1Names -notcontains (Get-BaseName $p)) { return $false }
  if ($p.CommandLine.Contains($V1Marker)) { return $true }
  return [bool]($p.CommandLine -match '(^|\s)-m\s+jarvisd(\s|$)')
}

# Everything below the given roots, by parent pid. A child cannot be older than its parent, so a
# parent pid that Windows reused after the real parent died is not followed.
function Add-Descendants([int[]]$rootIds, $into) {
  $frontier = @($rootIds)
  while ($frontier.Count -gt 0) {
    $next = @()
    foreach ($sp in $snapshot) {
      $parentId = [int]$sp.ParentProcessId
      $spid = [int]$sp.ProcessId
      if ($spid -eq $parentId -or $frontier -notcontains $parentId) { continue }
      if ($rootIds -contains $spid -or $into.Contains($spid)) { continue }
      $par = $byId[$parentId]
      if ($par -and $par.CreationDate -and $sp.CreationDate -and $sp.CreationDate -lt $par.CreationDate) { continue }
      $into[$spid] = 'child'
      $next += $spid
    }
    $frontier = $next
  }
}

# 0. Find the v1 daemon before anything is stopped. Stopping the task or writing state/KILL makes
# the daemon leave on its own, and a tree that has already gone cannot be listed afterwards.
$lockPath = Join-Path $StateDir 'daemon.lock'
if (Test-Path -LiteralPath $lockPath) {
  # The daemon holds one byte of this file through msvcrt.locking for its whole life. A lock
  # attempt that is refused proves a live holder; the lock is released again at once.
  $fs = $null
  try {
    $fs = New-Object System.IO.FileStream($lockPath, [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
    try { $fs.Lock(0, 1); $fs.Unlock(0, 1); Note 'daemon-lock' 'free' 'state/daemon.lock is not held' }
    catch { Note 'daemon-lock' 'held' 'state/daemon.lock is held by a live process' }
  } catch {
    Note 'daemon-lock' 'held' ('state/daemon.lock cannot be opened: ' + $_.Exception.Message)
  } finally { if ($fs) { $fs.Dispose() } }
} else {
  Note 'daemon-lock' 'absent' 'no state/daemon.lock'
}

# A plain hashtable: an ordered dictionary reads an integer key as a position, not as the pid.
$targets = @{}
$why = [System.Collections.ArrayList]::new()
$hbPath = Join-Path $StateDir 'heartbeat.json'
$hbPid = $null
if (Test-Path -LiteralPath $hbPath) {
  try { $hbPid = [int]((Get-Content -LiteralPath $hbPath -Raw | ConvertFrom-Json).pid) } catch { $hbPid = $null }
}
if ($hbPid) {
  $hp = $byId[$hbPid]
  if (-not $hp) {
    $null = $why.Add("heartbeat pid $hbPid is not running")
  } elseif (-not (Test-DaemonIdentity $hp)) {
    $null = $why.Add(("heartbeat pid {0} is {1}, not a jarvisd, left alone" -f $hbPid, $hp.Name))
  } else {
    $targets[$hbPid] = 'daemon'
    # The venv launcher (pythonw.exe in .venv) is the task's process and the daemon its child.
    $walk = $hp
    while ($walk) {
      $up = $byId[[int]$walk.ParentProcessId]
      if (-not $up -or [int]$up.ProcessId -eq [int]$walk.ProcessId) { break }
      if ($up.CreationDate -and $walk.CreationDate -and $walk.CreationDate -lt $up.CreationDate) { break }
      if (-not (Test-DaemonIdentity $up)) { break }
      $targets[[int]$up.ProcessId] = 'launcher'
      $walk = $up
    }
  }
} else {
  $null = $why.Add('no usable pid in state/heartbeat.json')
}

# Anything else wearing the daemon's command line (a heartbeat that went stale, a second copy),
# and any claude still carrying the isolated argv after its parent is gone. Owner and image
# name are both required, so a shell whose command line merely mentions the marker is not hit.
$me = $env:USERNAME
foreach ($sp in $snapshot) {
  if (Test-IsSelf $sp) { continue }
  if ($targets.Contains([int]$sp.ProcessId) -or -not $sp.CommandLine) { continue }
  if ($v1Names -notcontains (Get-BaseName $sp)) { continue }
  $role = $null
  if ($V1Marker -and $sp.CommandLine.Contains($V1Marker)) { $role = 'marker' }
  elseif ($ClaudeMarker -and $sp.CommandLine.Contains($ClaudeMarker)) { $role = 'claude-orphan' }
  if (-not $role) { continue }
  if ((Get-ProcOwner $sp) -ne $me) { continue }
  $targets[[int]$sp.ProcessId] = $role
}
if ($targets.Count -gt 0) { Add-Descendants ([int[]]@($targets.Keys)) $targets }
foreach ($k in @($targets.Keys)) {
  if ($byId[[int]$k] -and (Test-IsSelf $byId[[int]$k])) { $targets.Remove($k) }
}
if ($targets.Count -eq 0) {
  Note 'daemon-targets' 'none' (($why -join '; ') + $(if ($why.Count) { '; ' } else { '' }) + 'no v1 daemon process found')
} else {
  Note 'daemon-targets' 'found' ((@($why) + ("{0} process(es): {1}" -f $targets.Count, (($targets.Keys | ForEach-Object { "$_=" + $targets[$_] }) -join ' '))) -join '; ')
}

# 1. state/KILL: the daemon exits 3 on it and refuses to start again until a human removes it,
# so a task restart or a second logon cannot bring a killed daemon back.
$killFile = Join-Path $StateDir 'KILL'
if ($DryRun) {
  Note 'create-kill-file' 'would-create' $killFile
} else {
  try {
    if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Force -Path $StateDir | Out-Null }
    $oneLineReason = ($Reason -replace '[\r\n]+', ' ')
    [System.IO.File]::WriteAllText($killFile,
      ("kill-switch {0} reason={1} by={2}" -f (Get-Date).ToUniversalTime().ToString('o'), $oneLineReason, $TriggeredBy) + [Environment]::NewLine,
      [System.Text.UTF8Encoding]::new($false))
    Note 'create-kill-file' 'ok' $killFile
  } catch {
    Note 'create-kill-file' 'fail' $_.Exception.Message
  }
}

# 2. Stop the daemon scheduled task and disable it so it cannot restart itself.
try {
  $task = Get-ScheduledTask -TaskName $daemonTask -ErrorAction Stop
  if ($DryRun) {
    Note 'stop-task' 'would-stop' $daemonTask
  } else {
    Stop-ScheduledTask  -TaskName $daemonTask -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $daemonTask -ErrorAction Stop | Out-Null
    Note 'stop-task' 'ok' "$daemonTask stopped and disabled"
  }
} catch {
  Note 'stop-task' 'absent' "$daemonTask not registered yet"
}

# 3. Kill the daemon tree found in step 0. Task Scheduler normally tore it down in step 2 already
# (reported as gone); this is the belt for a daemon the task no longer owns.
$handled = @{}
foreach ($tpid in @($targets.Keys | Sort-Object { if ($targets[$_] -eq 'child') { 1 } else { 0 } })) {
  $handled[[int]$tpid] = $true
  $role = $targets[$tpid]
  $snap = $byId[[int]$tpid]
  $name = if ($snap) { $snap.Name } else { '?' }
  $live = Get-Process -Id $tpid -ErrorAction SilentlyContinue
  if ($DryRun) {
    Note 'kill-daemon-tree' 'would-kill' ("pid={0} name={1} role={2}" -f $tpid, $name, $role)
  } elseif (-not $live) {
    Note 'kill-daemon-tree' 'gone' ("pid={0} name={1} role={2} already ended" -f $tpid, $name, $role)
  } else {
    try {
      Stop-Process -Id $tpid -Force -ErrorAction Stop
      Note 'kill-daemon-tree' 'ok' ("pid={0} name={1} role={2}" -f $tpid, $name, $role)
    } catch {
      # The task teardown from step 2 is asynchronous: the process can end between the check
      # above and the kill. Only a process that is still there after a refused kill is a failure.
      if (Get-Process -Id $tpid -ErrorAction SilentlyContinue) {
        Note 'kill-daemon-tree' 'fail' ("pid={0} {1}" -f $tpid, $_.Exception.Message)
      } else {
        Note 'kill-daemon-tree' 'gone' ("pid={0} name={1} role={2} ended during the kill" -f $tpid, $name, $role)
      }
    }
  }
}

# 4. The jarvis-account branch, kept for the phases that move the daemon to that account.
# Selection is deliberately narrow: the owner must match the target account, and
# when a marker is supplied the command line must contain it. Pids already handled above are
# skipped so they are not reported twice.
try {
  $procs = $snapshot | ForEach-Object {
    $p = $_
    if (Test-IsSelf $p) { return }
    if ($handled.ContainsKey([int]$p.ProcessId)) { return }
    # The cheap image and command line tests come first: asking Windows for the owner of every
    # process on the machine takes tens of seconds.
    if ($CommandLineMarker) {
      if (-not ($p.CommandLine -and $p.CommandLine.Contains($CommandLineMarker))) { return }
    } elseif ($daemonProcessNames -notcontains (Get-BaseName $p)) {
      return
    }
    $owner = Get-ProcOwner $p
    if ($owner -and $owner -eq $TargetAccount) { $p }
  }

  if (-not $procs) {
    Note 'kill-processes' 'none' "no matching processes for account '$TargetAccount'"
  }
  foreach ($p in $procs) {
    if ($DryRun) {
      Note 'kill-processes' 'would-kill' ("pid={0} name={1}" -f $p.ProcessId, $p.Name)
    } else {
      try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Note 'kill-processes' 'ok' ("pid={0} name={1}" -f $p.ProcessId, $p.Name)
      } catch {
        Note 'kill-processes' 'fail' ("pid={0} {1}" -f $p.ProcessId, $_.Exception.Message)
      }
    }
  }
} catch {
  Note 'kill-processes' 'fail' $_.Exception.Message
}

# 5. Revoke egress. Phase 0 pre-creates the rule disabled; this enables it.
# The rule is keyed to the jarvis SID. For any other target it would constrain nothing, so it
# is reported as not applicable instead of being enabled for no effect.
if ($TargetAccount -ne 'jarvis') {
  Note 'revoke-egress' 'not-applicable' "egress rule is keyed to the jarvis SID, not '$TargetAccount'"
} elseif ($isAdmin) {
  try {
    $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction Stop
    if ($DryRun) {
      Note 'revoke-egress' 'would-enable' $ruleName
    } else {
      Enable-NetFirewallRule -DisplayName $ruleName -ErrorAction Stop
      Note 'revoke-egress' 'ok' "$ruleName enabled"
    }
  } catch {
    Note 'revoke-egress' 'absent' "$ruleName not created yet, see docs/phase0-runbook.md"
  }
} else {
  Note 'revoke-egress' 'skipped' 'needs elevation'
}

# 6. Full scope only: drop remote access. This cuts your own sessions.
if ($Scope -eq 'Full') {
  if ($isAdmin) {
    foreach ($svc in @('RustDesk')) {
      try {
        if (Get-Service -Name $svc -ErrorAction Stop) {
          if ($DryRun) { Note 'stop-service' 'would-stop' $svc }
          else { Stop-Service -Name $svc -Force -ErrorAction Stop; Note 'stop-service' 'ok' $svc }
        }
      } catch { Note 'stop-service' 'absent' $svc }
    }
    $ssh = Get-Process sshd -ErrorAction SilentlyContinue
    if ($ssh) {
      if ($DryRun) { Note 'drop-ssh' 'would-kill' ("{0} sshd processes" -f $ssh.Count) }
      else { $ssh | Stop-Process -Force -ErrorAction SilentlyContinue; Note 'drop-ssh' 'ok' ("{0} killed" -f $ssh.Count) }
    } else { Note 'drop-ssh' 'none' 'no sshd running' }

    $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $ts) {
      if ($DryRun) { Note 'tailscale-down' 'would-run' $ts }
      else { & $ts down 2>&1 | Out-Null; Note 'tailscale-down' 'ok' 'tailnet down' }
    } else { Note 'tailscale-down' 'absent' 'tailscale.exe not found' }
  } else {
    Note 'full-scope' 'skipped' 'needs elevation'
  }
}

# 7. Audit. Always written, even on failure.
$entry = [ordered]@{
  ts           = (Get-Date).ToUniversalTime().ToString('o')
  event        = 'killswitch'
  scope        = $Scope
  dry_run      = [bool]$DryRun
  elevated     = $isAdmin
  reason       = $Reason
  triggered_by = $TriggeredBy
  target       = $TargetAccount
  marker       = $CommandLineMarker
  actions      = $actions
}
try {
  $dir = Split-Path -Parent $AuditPath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  # Append bytes directly: Windows PowerShell 5.1 writes a BOM for -Encoding utf8,
  # which corrupts line 1 of a JSONL file for any standard reader. This script runs
  # under 5.1 when the elevated scheduled task invokes it, so it matters here.
  [System.IO.File]::AppendAllText(
    $AuditPath,
    ($entry | ConvertTo-Json -Depth 6 -Compress) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false))
  Write-Host "audit: $AuditPath"
} catch {
  Write-Warning ("audit write FAILED: {0}" -f $_.Exception.Message)
}

$failed = @($actions | Where-Object { $_.result -eq 'fail' }).Count
if ($failed -gt 0) { exit 1 } else { exit 0 }
