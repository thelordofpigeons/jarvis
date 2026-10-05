<#
.SYNOPSIS
  Register (or remove) the JarvisDaemon scheduled task. Design section 10.

.DESCRIPTION
  Run once from a normal PowerShell as the owner account. No elevation: the task runs with
  RunLevel Limited under an Interactive logon, because the Claude OAuth profile and the toast
  both need the real desktop session (design D1; an S4U logon cannot show a toast).

  Idempotent: Register-ScheduledTask -Force replaces an existing JarvisDaemon, so a second run
  changes nothing that matters. JarvisKillSwitch stops and disables this task by name, and
  nothing here touches that task.

  WakeToRun stays off on purpose. If the machine sleeps through 06:30 the digest is built at
  wake or logon and its front matter says late: true after 12:00.

  jarvis install-task prints the same block with this checkout's paths.

.PARAMETER Unregister
  Remove the JarvisDaemon task instead of registering it.

.PARAMETER WhatIf
  Print what would happen and register nothing.

.EXAMPLE
  powershell -File deploy\register-jarvisd-task.ps1 -WhatIf
.EXAMPLE
  powershell -File deploy\register-jarvisd-task.ps1
.EXAMPLE
  powershell -File deploy\register-jarvisd-task.ps1 -Unregister
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$taskName = 'JarvisDaemon'
$root     = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
# pythonw has no console window, so everything the daemon prints goes to files under logs/.
$py       = Join-Path $root '.venv\Scripts\pythonw.exe'

function Show-TaskInfo {
    # Not every state has a task to describe (WhatIf, or just unregistered), so this must not throw.
    try {
        Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop | Format-List *
    } catch {
        Write-Host "Get-ScheduledTaskInfo ${taskName}: no task registered."
    }
}

if ($Unregister) {
    if ($PSCmdlet.ShouldProcess($taskName, 'Unregister scheduled task')) {
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Host "Unregistered $taskName."
        } else {
            Write-Host "$taskName was not registered. Nothing to do."
        }
    }
    Show-TaskInfo
    return
}

if (-not (Test-Path -LiteralPath $py)) {
    if ($WhatIfPreference) {
        Write-Warning "pythonw.exe not found at $py. Run deploy\setup-venv.ps1 before registering for real."
    } else {
        throw "pythonw.exe not found at $py. Run deploy\setup-venv.ps1 first."
    }
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$act  = New-ScheduledTaskAction -Execute $py -Argument '-m jarvisd serve --task' -WorkingDirectory $root
$t1   = New-ScheduledTaskTrigger -AtLogOn -User $user
$t2   = New-ScheduledTaskTrigger -Daily -At 06:00
# The cmdlet stamps a trailing Z (UTC) on StartBoundary, which fires 06:00 UTC, not local. A boundary
# with no zone suffix is read as local time by Task Scheduler, so overwrite it.
$t2.StartBoundary = (Get-Date -Hour 6 -Minute 0 -Second 0).ToString('yyyy-MM-dd\THH:mm:ss')
$set  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2)
$prin = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Write-Host "Plan for ${taskName}:"
Write-Host "  user       $user (Interactive logon, RunLevel Limited, no elevation)"
Write-Host "  action     $py -m jarvisd serve --task"
Write-Host "  start in   $root"
Write-Host "  triggers   at logon, and daily at 06:00 (restarts a dead daemon; state\daemon.lock is the second guard)"
Write-Host "  settings   IgnoreNew, no time limit, restart 5 times every 2 minutes, WakeToRun off"

if ($PSCmdlet.ShouldProcess($taskName, 'Register scheduled task')) {
    Register-ScheduledTask -TaskName $taskName -Action $act -Trigger @($t1, $t2) -Settings $set -Principal $prin -Description 'JARVIS v1 option 2 resident daemon (morning digest). Observe-only. Stopped and disabled by JarvisKillSwitch.' -Force | Out-Null
    Write-Host "Registered $taskName."
}

Show-TaskInfo
