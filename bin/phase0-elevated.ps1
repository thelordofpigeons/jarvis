<#
Phase 0 elevated setup. Run once, as Administrator.

Idempotent: every step checks current state first, so re-running is safe and
reports "already" instead of failing.

What it does NOT do, deliberately:
  - reboot (TdrDelay needs one, that is your call)
  - run the kill switch at Full scope (that severs remote access)
  - create Credential Manager entries (per-user store, must be done as jarvis)

Writes a transcript to logs\phase0-elevated.log and one JSON line per step to
logs\phase0-elevated.jsonl.
#>
[CmdletBinding()]
param(
  [switch] $GrantTelosRead = $true,   # non-sensitive TELOS tiers, see runbook step 2
  [int]    $TdrDelaySeconds = 60,
  # The human account that owns the vault and the kill-switch task. Elevating with a
  # different administrator account? Pass these two explicitly.
  [string] $OwnerHome    = $env:USERPROFILE,
  [string] $OwnerAccount = $env:USERNAME
)

$ErrorActionPreference = 'Continue'
$root     = Split-Path -Parent $PSScriptRoot
$logDir   = Join-Path $root 'logs'
$txtLog   = Join-Path $logDir 'phase0-elevated.log'
$jsonLog  = Join-Path $logDir 'phase0-elevated.jsonl'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$HOMEDIR  = $OwnerHome
$BRAIN    = Join-Path $HOMEDIR 'brain'
$ACCOUNT  = 'jarvis'
$RULE     = 'JARVIS-daemon-egress-block'
$TASK     = 'JarvisKillSwitch'
$steps    = [System.Collections.ArrayList]::new()

# Append without a byte-order mark. Windows PowerShell 5.1 writes UTF-8 WITH a BOM
# for -Encoding utf8, which lands a stray marker on line 1 and breaks any standard
# JSONL reader (json.loads raises on it). Write bytes ourselves instead.
$script:NoBom = [System.Text.UTF8Encoding]::new($false)
function Append-Utf8([string]$path, [string]$text) {
  [System.IO.File]::AppendAllText($path, $text + [Environment]::NewLine, $script:NoBom)
}

function Step([string]$name, [string]$status, [string]$detail = '') {
  $null = $steps.Add([ordered]@{ step = $name; status = $status; detail = $detail })
  $line = "[{0}] {1} {2}" -f $status.ToUpper(), $name, $detail
  Write-Host $line
  Append-Utf8 $txtLog ("{0} {1}" -f (Get-Date -Format o), $line)
  Append-Utf8 $jsonLog (([ordered]@{ ts = (Get-Date).ToUniversalTime().ToString('o')
                                     step = $name; status = $status
                                     detail = $detail } | ConvertTo-Json -Compress))
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Write-Host 'This script must run elevated. Nothing was changed.' -ForegroundColor Red
  exit 2
}
Append-Utf8 $txtLog ("`n===== phase0-elevated run {0} =====" -f (Get-Date -Format o))

# ---------------------------------------------------------------- 1. account
$user = Get-LocalUser -Name $ACCOUNT -ErrorAction SilentlyContinue
if ($user) {
  Step 'account-create' 'already' "$ACCOUNT exists, SID $($user.SID.Value)"
} else {
  Write-Host ''
  Write-Host "Set a password for the '$ACCOUNT' daemon account." -ForegroundColor Cyan
  Write-Host "You will need it once, to sign in as jarvis and add Credential Manager entries." -ForegroundColor Cyan
  $pw = Read-Host -AsSecureString "Password for $ACCOUNT"
  try {
    $user = New-LocalUser -Name $ACCOUNT -Password $pw -FullName 'JARVIS daemon' `
              -Description 'Unprivileged account for the JARVIS local daemon' `
              -PasswordNeverExpires -ErrorAction Stop
    Step 'account-create' 'ok' "created, SID $($user.SID.Value)"
  } catch {
    Step 'account-create' 'fail' $_.Exception.Message
  }
}

# ------------------------------------------------------- 2. group membership
if ($user) {
  $inGroups = @()
  foreach ($g in Get-LocalGroup) {
    $m = Get-LocalGroupMember -Group $g.Name -ErrorAction SilentlyContinue |
         Where-Object { $_.SID.Value -eq $user.SID.Value }
    if ($m) { $inGroups += $g.Name }
  }
  $bad = $inGroups | Where-Object { $_ -ne 'Users' }
  foreach ($b in $bad) {
    try {
      Remove-LocalGroupMember -Group $b -Member $ACCOUNT -ErrorAction Stop
      Step 'account-groups' 'ok' "removed from $b"
    } catch {
      Step 'account-groups' 'fail' "could not remove from $b : $($_.Exception.Message)"
    }
  }

  # New-LocalUser adds the account to NO group. Membership in Users is required:
  # "Allow log on locally" defaults to Administrators plus Users, so without it you
  # cannot sign in as jarvis to create the Credential Manager entries, and the
  # Task Scheduler logon for the daemon fails too. The first run of this script
  # missed this, which is why it reported "member of:" with nothing after it.
  if ($inGroups -notcontains 'Users') {
    try {
      Add-LocalGroupMember -Group 'Users' -Member $ACCOUNT -ErrorAction Stop
      Step 'account-groups' 'ok' 'added to Users'
    } catch {
      Step 'account-groups' 'fail' "could not add to Users: $($_.Exception.Message)"
    }
  } else {
    Step 'account-groups' 'already' ("member of: " + ($inGroups -join ', '))
  }
}

# ------------------------------------------------------------- 3. vault ACLs
function Set-Acl-Grant([string]$path, [string]$perm, [string]$label) {
  if (-not (Test-Path $path)) { Step "acl-$label" 'skip' "absent: $path"; return }
  $out = & icacls $path /grant "${ACCOUNT}:$perm" 2>&1
  if ($LASTEXITCODE -eq 0) { Step "acl-$label" 'ok' "$perm on $path" }
  else { Step "acl-$label" 'fail' (($out | Out-String).Trim()) }
}
function Set-Acl-Deny([string]$path, [string]$label) {
  if (-not (Test-Path $path)) { Step "deny-$label" 'skip' "absent: $path"; return }
  $out = & icacls $path /deny "${ACCOUNT}:(OI)(CI)(F)" 2>&1
  if ($LASTEXITCODE -eq 0) { Step "deny-$label" 'ok' $path }
  else { Step "deny-$label" 'fail' (($out | Out-String).Trim()) }
}

if ($user) {
  # Traverse-only on the profile and brain root so the grants below are reachable
  # without exposing sibling folders.
  foreach ($p in @($HOMEDIR, $BRAIN)) {
    $out = & icacls $p /grant "${ACCOUNT}:(RX)" 2>&1
    if ($LASTEXITCODE -eq 0) { Step 'acl-traverse' 'ok' "(RX) this-folder-only on $p" }
    else { Step 'acl-traverse' 'fail' (($out | Out-String).Trim()) }
  }

  Set-Acl-Grant (Join-Path $BRAIN 'raw')       '(OI)(CI)(RX)' 'raw-read'
  Set-Acl-Grant (Join-Path $BRAIN 'sessions')  '(OI)(CI)(RX)' 'sessions-read'
  Set-Acl-Grant (Join-Path $BRAIN 'insights')  '(OI)(CI)(RX)' 'insights-read'
  Set-Acl-Grant (Join-Path $BRAIN 'raw\jarvis') '(OI)(CI)(M)' 'raw-jarvis-write'
  Set-Acl-Grant (Join-Path $root 'queue')      '(OI)(CI)(M)'  'queue-write'
  Set-Acl-Grant (Join-Path $root 'logs')       '(OI)(CI)(M)'  'logs-write'
  Set-Acl-Grant (Join-Path $root 'models')     '(OI)(CI)(RX)' 'models-read'
  Set-Acl-Grant (Join-Path $root 'jarvis.toml') '(R)'         'config-read'

  if ($GrantTelosRead) {
    Set-Acl-Grant (Join-Path $BRAIN 'telos') '(OI)(CI)(RX)' 'telos-read'
  } else {
    Step 'acl-telos-read' 'skip' 'not granted by request'
  }

  # Denials last: they win over any inherited or explicit grant.
  Set-Acl-Deny (Join-Path $BRAIN 'telos\sensitive') 'telos-sensitive'
  Set-Acl-Deny (Join-Path $BRAIN 'notes')           'notes'
  Set-Acl-Deny (Join-Path $HOMEDIR 'Documents\Work') 'documents-work'
  Set-Acl-Deny (Join-Path $HOMEDIR '.claude')        'claude-config'
  Set-Acl-Deny (Join-Path $HOMEDIR '.ssh')           'ssh-keys'
}

# ------------------------------------------------- 4. TdrDelay + driver pin
$gk = 'HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers'
foreach ($n in @('TdrDelay','TdrDdiDelay')) {
  try {
    $cur = (Get-ItemProperty -Path $gk -Name $n -ErrorAction SilentlyContinue).$n
    if ($cur -eq $TdrDelaySeconds) { Step "reg-$n" 'already' "$n = $cur" }
    else {
      New-ItemProperty -Path $gk -Name $n -PropertyType DWord -Value $TdrDelaySeconds -Force -ErrorAction Stop | Out-Null
      Step "reg-$n" 'ok' ("{0}: {1} -> {2} (reboot required)" -f $n, $(if ($null -eq $cur) {'unset'} else {$cur}), $TdrDelaySeconds)
    }
  } catch { Step "reg-$n" 'fail' $_.Exception.Message }
}
try {
  $dk = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching'
  $cur = (Get-ItemProperty -Path $dk -Name SearchOrderConfig -ErrorAction SilentlyContinue).SearchOrderConfig
  if ($cur -eq 0) { Step 'reg-driver-pin' 'already' 'SearchOrderConfig = 0' }
  else {
    Set-ItemProperty -Path $dk -Name SearchOrderConfig -Value 0 -ErrorAction Stop
    Step 'reg-driver-pin' 'ok' ("SearchOrderConfig: {0} -> 0" -f $cur)
  }
} catch { Step 'reg-driver-pin' 'fail' $_.Exception.Message }

$gpu = Get-CimInstance Win32_VideoController | Select-Object -First 1
Step 'driver-version-recorded' 'ok' ("{0} driver {1}" -f $gpu.Name, $gpu.DriverVersion)

# ----------------------------------------------- 5. kill-switch scheduled task
try {
  $existing = Get-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue
  if ($existing) {
    Step 'killswitch-task' 'already' "$TASK registered"
  } else {
    $ks = Join-Path $root 'bin\kill-switch.ps1'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
      -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Scope Daemon -Reason watchdog-trip' -f $ks)
    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:COMPUTERNAME, $OwnerAccount) `
      -LogonType S4U -RunLevel Highest
    Register-ScheduledTask -TaskName $TASK -Action $action -Principal $principal `
      -Description 'JARVIS kill switch, elevated. Stops the daemon and revokes its egress.' `
      -ErrorAction Stop | Out-Null
    Step 'killswitch-task' 'ok' "$TASK registered, RunLevel Highest, S4U"
  }
} catch { Step 'killswitch-task' 'fail' $_.Exception.Message }

# --------------------------------------------------- 6. egress block rule
try {
  $r = Get-NetFirewallRule -DisplayName $RULE -ErrorAction SilentlyContinue
  if ($r) {
    Step 'firewall-rule' 'already' ("$RULE exists, enabled=" + $r.Enabled)
  } elseif ($user) {
    $sid = $user.SID.Value
    New-NetFirewallRule -DisplayName $RULE -Direction Outbound -Action Block -Enabled False `
      -LocalUser ("D:(A;;CC;;;{0})" -f $sid) `
      -Description 'Enabled by the JARVIS kill switch to revoke daemon egress' `
      -ErrorAction Stop | Out-Null
    Step 'firewall-rule' 'ok' "$RULE created, disabled"
  }
  # Verify the SDDL actually bound to the account, this form is easy to get wrong.
  $f = Get-NetFirewallRule -DisplayName $RULE -ErrorAction SilentlyContinue |
       Get-NetFirewallSecurityFilter -ErrorAction SilentlyContinue
  if ($f) {
    $bound = $f.LocalUser
    if ($user -and $bound -like ("*" + $user.SID.Value + "*")) {
      Step 'firewall-sddl-verified' 'ok' $bound
    } else {
      Step 'firewall-sddl-verified' 'fail' ("LocalUser = '" + $bound + "', expected the jarvis SID")
    }
  } else {
    Step 'firewall-sddl-verified' 'fail' 'no security filter returned'
  }
} catch { Step 'firewall-rule' 'fail' $_.Exception.Message }

# ------------------------------------------------------------------ summary
$fails = @($steps | Where-Object { $_.status -eq 'fail' })
Write-Host ''
Write-Host ("phase0-elevated: {0} steps, {1} failed" -f $steps.Count, $fails.Count)
if ($fails.Count) { $fails | ForEach-Object { Write-Host ("  FAIL {0}: {1}" -f $_.step, $_.detail) } }
Write-Host ''
Write-Host 'Still yours to do:'
Write-Host '  1. Reboot, so TdrDelay takes effect.'
Write-Host '  2. Sign in as jarvis once and add the Credential Manager entries (runbook step 3).'
Write-Host '  3. Test the kill switch at Full scope while sitting at the machine (runbook step 8).'
Write-Host ("transcript: {0}" -f $txtLog)
exit $(if ($fails.Count) { 1 } else { 0 })
