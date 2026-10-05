param([string]$Title = "JARVIS", [string]$Message = "")

# Copy of ~/.claude/hooks/notify.ps1 with the AppUserModelId changed to JARVIS, so a digest
# toast is not labelled "Claude Code". Called by jarvisd/notify.py with -Title and -Message.

# Register app for banner toast notifications (idempotent)
$regPath = "HKCU:\SOFTWARE\Classes\AppUserModelId\JARVIS"
if (-not (Test-Path $regPath)) {
    New-Item -Path $regPath -Force | Out-Null
    New-ItemProperty -Path $regPath -Name "DisplayName" -Value "JARVIS" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $regPath -Name "ShowInSettings" -Value 1 -PropertyType DWord -Force | Out-Null
}

[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

$safeTitle   = [System.Security.SecurityElement]::Escape($Title)
$safeMessage = [System.Security.SecurityElement]::Escape($Message)
$toastXml = "<toast><visual><binding template='ToastGeneric'><text>$safeTitle</text><text>$safeMessage</text></binding></visual></toast>"

$xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
$xml.LoadXml($toastXml)

[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("JARVIS").Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml)
)
