<#
.SYNOPSIS
  Create the "JARVIS Hub" and "JARVIS Face" shortcuts on the Desktop and in the Start menu.

.DESCRIPTION
  Both shortcuts run a launcher under bin\ minimised (the console window of the .cmd stays out of
  sight) with the lantern icon from bin\jarvis.ico:
    JARVIS Hub   bin\hub.cmd          the cockpit at http://127.0.0.1:<port>/ in an app window; starts the hub if needed
    JARVIS Face  bin\face-window.cmd  the avatar at /face in a small app window; needs the hub running
  The Desktop is whatever Windows says it is (a OneDrive folder on this machine). Re-running replaces
  the two .lnk files and nothing else.

.PARAMETER Remove
  Delete the two shortcuts instead of creating them.

.EXAMPLE
  powershell -File deploy\make-shortcuts.ps1
.EXAMPLE
  powershell -File deploy\make-shortcuts.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$root    = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$icon    = Join-Path $root 'bin\jarvis.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'JARVIS'

$shortcuts = @(
    @{ Name = 'JARVIS Hub';  Target = Join-Path $root 'bin\hub.cmd';         Description = 'JARVIS hub: digest, runs, inbox, face. Starts the hub when it is not running.' },
    @{ Name = 'JARVIS Face'; Target = Join-Path $root 'bin\face-window.cmd'; Description = 'The JARVIS avatar in a small window (needs the hub running).' }
)

if ($Remove) {
    foreach ($s in $shortcuts) {
        foreach ($dir in @($desktop, $startMenu)) {
            $lnk = Join-Path $dir ($s.Name + '.lnk')
            if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force; Write-Host "Removed $lnk" }
        }
    }
    if ((Test-Path -LiteralPath $startMenu) -and -not (Get-ChildItem -LiteralPath $startMenu)) {
        Remove-Item -LiteralPath $startMenu -Force
    }
    return
}

if (-not (Test-Path -LiteralPath $icon)) { throw "icon not found at $icon" }
foreach ($s in $shortcuts) {
    if (-not (Test-Path -LiteralPath $s.Target)) { throw "launcher not found at $($s.Target)" }
}
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null

$shell = New-Object -ComObject WScript.Shell
foreach ($s in $shortcuts) {
    foreach ($dir in @($desktop, $startMenu)) {
        $lnk = Join-Path $dir ($s.Name + '.lnk')
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath = $s.Target
        $sc.WorkingDirectory = $root
        $sc.IconLocation = "$icon,0"
        $sc.Description = $s.Description
        $sc.WindowStyle = 7   # minimised: the launcher's console never comes to the front
        $sc.Save()
        Write-Host "Wrote $lnk"
    }
}
