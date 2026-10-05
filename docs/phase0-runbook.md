# Phase 0 runbook: isolation

Everything here needs Administrator, which the assistant session does not have, so
these are yours to run. Open an elevated PowerShell (Win+X, Terminal as Admin) and
work top to bottom. In Claude Code you can also prefix a line with `!` to run it in
the session, but the elevated steps still need an elevated shell.

Source of truth: `~/brain/raw/2026-09-17-local-models-sota-research.md` section 9a,
9c and 13. Config contract: `~/jarvis/jarvis.toml`.

Each step says how to undo it.

---

## 1. Create the daemon account

Standard user, no membership in your groups. You type the password, it is never
written to a file and never passes through the assistant.

```powershell
$pw = Read-Host -AsSecureString "Password for the jarvis account"
New-LocalUser -Name 'jarvis' -Password $pw -FullName 'JARVIS daemon' `
  -Description 'Unprivileged account for the JARVIS local daemon' -PasswordNeverExpires
# Members of Users only. Confirm it is in nothing else:
Get-LocalGroup | ForEach-Object {
  $g=$_.Name; Get-LocalGroupMember -Group $g -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -like '*\jarvis' } | ForEach-Object { "$g" }
}
```

Expected output: `Users` and nothing more.

Undo: `Remove-LocalUser -Name jarvis`

## 2. Vault access, the one real decision in phase 0

The daemon runs as a different user, so by default it can read nothing in your
profile. Access has to be granted deliberately, which is the right default.

Grant the minimum the digest and consolidation jobs need:

```powershell
$B = "C:\Users\<owner>\brain"
# Read where it needs to observe
icacls "$B\raw"       /grant 'jarvis:(OI)(CI)(RX)'
icacls "$B\sessions"  /grant 'jarvis:(OI)(CI)(RX)'
icacls "$B\insights"  /grant 'jarvis:(OI)(CI)(RX)'
# Write only where section 4c allows
New-Item -ItemType Directory -Force -Path "$B\raw\jarvis" | Out-Null
icacls "$B\raw\jarvis" /grant 'jarvis:(OI)(CI)(M)'
# Hard denials, these win over any grant
icacls "$B\telos\sensitive"              /deny  'jarvis:(OI)(CI)(F)'
icacls "$B\notes"                        /deny  'jarvis:(OI)(CI)(F)'
icacls "C:\Users\<owner>\Documents\<work>" /deny 'jarvis:(OI)(CI)(F)'
```

**Decide before running:** should the daemon read `brain\telos` (the non-sensitive
tiers)? Identity context is the entire point of TELOS, so the router will classify
much better with it, but it also means your identity files sit inside the daemon's
reach. Recommended: grant read on `telos` while keeping the `telos\sensitive` denial
above, because gate 1 already blocks sensitive content from leaving the machine.

```powershell
# Only if you accept the above
icacls "C:\Users\<owner>\brain\telos" /grant 'jarvis:(OI)(CI)(RX)'
```

Undo any grant: same command with `/remove:g jarvis`. Undo a denial: `/remove:d jarvis`.

## 3. Credential Manager entries

These must be created while logged in **as jarvis**, because the store is per-user.
Sign in to the jarvis account once, open PowerShell there, and add the entries. Do
not paste secret values into this session.

```powershell
# as jarvis
cmdkey /generic:"jarvis/llama-server-api-key" /user:"jarvis" /pass
# repeat later for ntfy, Slack and ClickUp tokens when those phases arrive
cmdkey /list | Select-String jarvis
```

Generate the llama-server key with something like:
`[Convert]::ToBase64String((1..32|%{Get-Random -Max 256}))`

Undo: `cmdkey /delete:"jarvis/llama-server-api-key"`

## 4. Raise TdrDelay and pin the driver

Vulkan on RDNA3 under Windows has documented DeviceLost and TDR failures under
sustained load (llama.cpp issue 22646). Current state on this machine: TdrDelay is
unset, so the default 2 seconds applies, and Windows driver auto-install is on.

```powershell
$k = 'HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers'
New-ItemProperty -Path $k -Name TdrDelay      -PropertyType DWord -Value 60 -Force
New-ItemProperty -Path $k -Name TdrDdiDelay   -PropertyType DWord -Value 60 -Force
# Stop Windows replacing the pinned GPU driver
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching' `
  -Name SearchOrderConfig -Value 0
```

A reboot is required for TdrDelay. Record the pinned driver version, currently
`32.0.31041.1004`, in the session note so a later regression is attributable.

Undo: `Remove-ItemProperty -Path $k -Name TdrDelay,TdrDdiDelay` and set
`SearchOrderConfig` back to 1.

## 5. Register the elevated kill-switch task

The watchdog runs unelevated, so the firewall, service and Tailscale steps of the
kill switch need a task registered once with highest privileges. After this the
watchdog can trip it with `schtasks /run /tn JarvisKillSwitch`.

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\<owner>\jarvis\bin\kill-switch.ps1" -Scope Daemon -Reason watchdog-trip'
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\<owner>" `
  -LogonType S4U -RunLevel Highest
Register-ScheduledTask -TaskName 'JarvisKillSwitch' -Action $action -Principal $principal `
  -Description 'JARVIS kill switch, elevated. Trips daemon stop plus egress revoke.'
```

Undo: `Unregister-ScheduledTask -TaskName JarvisKillSwitch -Confirm:$false`

## 6. Pre-create the egress block rule, disabled

The kill switch enables this rule rather than creating it under pressure.

```powershell
$sid = (Get-LocalUser jarvis).SID.Value
New-NetFirewallRule -DisplayName 'JARVIS-daemon-egress-block' `
  -Direction Outbound -Action Block -Enabled False `
  -LocalUser "D:(A;;CC;;;$sid)" `
  -Description 'Enabled by the JARVIS kill switch to revoke daemon egress'
```

Verify the SDDL took effect, since the `-LocalUser` form is easy to get wrong:

```powershell
Get-NetFirewallRule -DisplayName 'JARVIS-daemon-egress-block' |
  Get-NetFirewallSecurityFilter | Select-Object LocalUser
```

Undo: `Remove-NetFirewallRule -DisplayName 'JARVIS-daemon-egress-block'`

## 7. Install sandbox-runtime and grant its ACLs

`srt` wraps every daemon-initiated shell, browser and filesystem action. Its Windows
backend runs children as a separate `srt-sandbox` account and filters egress through
the Windows Filtering Platform. Note the documented gap: **srt does not fence DNS on
Windows**, so the jarvis account also needs a filtering resolver or a hosts allowlist.

```powershell
npm install -g @anthropic-ai/sandbox-runtime
srt --version
```

Then provision and test the ACL grants so srt can see the daemon's tools, which is
listed as an open gap in section 12 and must be proven end to end before the daemon
is trusted. Record the result in the session note.

## 8. Verify the kill switch at full scope

Daemon scope is already verified by `tests\hostile-sim.ps1`, 7 of 7 checks, run
unelevated. Two things remain untestable without you:

- the elevated branches: firewall enable, RustDesk stop, sshd drop, `tailscale down`
- Full scope end to end

**Full scope cuts your own remote access**, so run it only while physically at the
machine, never over SSH or RustDesk.

```powershell
# dry run first, safe from anywhere
powershell -NoProfile -File "C:\Users\<owner>\jarvis\bin\kill-switch.ps1" -Scope Full -DryRun
# the real thing, at the machine only
powershell -NoProfile -File "C:\Users\<owner>\jarvis\bin\kill-switch.ps1" -Scope Full -Reason phase0-verification
# then bring connectivity back
tailscale up
Start-Service RustDesk
```

Check the audit trail afterwards:

```powershell
Get-Content C:\Users\<owner>\jarvis\logs\killswitch.jsonl | Select-Object -Last 1 | ConvertFrom-Json
```

---

## Phase 0 exit criteria

- [ ] `jarvis` account exists, in `Users` only
- [ ] vault ACLs granted and denials verified, telos decision recorded
- [ ] llama-server API key in Credential Manager under the jarvis account
- [ ] TdrDelay 60 applied, rebooted, driver version recorded, auto-update off
- [ ] `JarvisKillSwitch` task registered and runnable
- [ ] egress block rule exists, disabled, SDDL verified
- [ ] srt installed, ACL grants proven end to end, DNS allowlist decided
- [ ] kill switch verified at Full scope at the machine, audit line present

## Known constraint before phase 1

C: has 58 GB free and is the only drive. A gpt-oss-20b GGUF is roughly 12 GB and a
Qwen3-30B-A3B IQ4_XS is roughly 17 GB, so pulling both for the phase 1 bench
decision leaves under 30 GB. Pull gpt-oss-20b first, bench it, and only pull Qwen3
if the benchmark actually leaves the decision open.
