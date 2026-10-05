# Sandbox policy notes

Explains `srt-settings.json`. The JSON itself carries no comments on purpose: the
sandbox config schemas use `.strict()`, so an extra key that is not in the schema
can be rejected outright, and a settings file that fails to validate is a hard
error rather than a fallback to defaults.

Invoke it by absolute path, never by bare `srt`:

```powershell
& "C:\Users\<owner>\AppData\Roaming\npm\srt.cmd" `
    --settings "C:\Users\<owner>\jarvis\srt-settings.json" -- <command>
```

## The PATH trap

Python's subtitle library installs a script also called `srt` into
`Python312\Scripts`, which sits earlier on PATH than the npm binary on this
machine. A bare `srt` runs the subtitle parser and exits successfully, so a daemon
that shells out to `srt` would appear to work while sandboxing nothing at all. The
resolved path is pinned in `jarvis.toml` under `[sandbox] srt_path`.

## Two isolation layers, not one

They are easy to conflate:

- **`jarvis`** is the account the daemon itself runs as. Its reach is set by NTFS
  ACLs, applied by `bin/phase0-elevated.ps1`.
- **`srt-sandbox`** is a separate account that srt runs each sandboxed child as.
  Its reach is set by this settings file, which srt turns into additive, inheriting
  ACEs at initialize time, plus a Windows Filtering Platform egress fence keyed on
  that account's SID.

So a daemon action passes through both. Loosening one does not loosen the other.

## Why the rules are shaped this way

Read and write use opposite precedence, which is the main thing to remember:

- **Reads are deny-then-allow.** Reading is permitted everywhere by default, so the
  home directory is denied wholesale and only the vault regions the digest and
  consolidation jobs need are re-allowed. `allowRead` beats `denyRead`, except that
  a `denyRead` entry more specific than the `allowRead` region containing it stays
  denied. That is what keeps `telos/sensitive` and `notes` shut while `telos` is
  readable.
- **Writes are allow-then-deny.** `denyWrite` beats `allowWrite`, so the three
  writable paths are listed and the vault's definition files are denied on top as
  defense in depth. This matches section 4c: the daemon writes only to
  `brain/raw/jarvis` and its own queue and logs.
- **`bin/`, `jarvis.toml` and this policy file are write-denied.** A sandboxed
  action must not be able to edit the daemon's own code, its config, or the rules
  that constrain it.
- **No egress at all.** `allowedDomains` is empty, which under an allow-only model
  means the sandbox reaches nothing. Phase 0 and 1 need none: models are downloaded
  by the human through the audit steps. Phase 3 adds the Anthropic API host when
  the Claude bridge lands, and that is the moment to re-read this file.

## Known gap, stated not hidden

srt does not fence DNS on Windows. The egress fence blocks outbound connections
from the sandbox account except loopback to the proxy port range, but name
resolution is not constrained, so the `jarvis` account still needs a filtering
resolver or a hosts allowlist. Open item, carried from report section 9a.

## Open blocker: srt cannot verify its fence unelevated

Installed 2026-09-22 and provisioned correctly: the `srt-sandbox` account exists
(in `Users` and `sandbox-runtime-users`), the credential blob is in
`HKLM\SOFTWARE\sandbox-runtime\Cred`, and the installer reported four WFP filters
on the port range 60080 to 60089.

A sandboxed probe from a normal, non-elevated shell then failed:

```
Error: WFP egress fence could not be verified - `srt-win wfp verify` exited 1
  srt-win: error: spawn runner for egress probe:
  CreateProcessWithLogonW(srt-sandbox): Access is denied. (0x80070005)
  - ensure the Secondary Logon service (seclogon) is running.
```

The suggested cause is not the real one: `seclogon` is running, start type Manual,
and `srt-sandbox` holds the interactive logon right through `Users`. Re-running
`windows-install` unelevated reports `WFP: cannot-read, 0 filters`, while the
elevated run reported four. So the filter set is almost certainly fine and it is
the *verification* that needs Administrator, which then fails the whole wrap.

### What was ruled out, 2026-09-22

Four hypotheses tested and eliminated, so nobody repeats this:

| Hypothesis | Result |
|---|---|
| Secondary Logon service stopped, as the error suggests | running, start type Manual |
| Sandbox account lacks the interactive logon right | it is in `Users` and `sandbox-runtime-users` |
| The caller must be elevated | fails identically from an elevated shell |
| Stored credential out of sync with the account password | elevated reinstall rotates and reconciles, still fails |

The elevated reinstall reports `WFP: installed, 4 filters, port range 60080-60089`,
so the fence itself is in place. Debug output (`-d`) adds only that it resolved
`vendor\srt-win\x64\srt-win.exe` and saw the four filters before the same verify
step failed. The failure is inside `srt-win wfp verify`, which cannot spawn its
egress probe as the sandbox account.

### Conclusion and cost

Treated as an upstream defect in alpha Windows support, not a misconfiguration
here. Investigation stopped deliberately rather than spending more time inside
another project's alpha code.

- **Good news worth stating:** srt fails closed. It refused to run the command
  rather than running it unsandboxed. A tool that silently degraded here would be
  far more dangerous than one that errors.
- **Phase 0 exit criterion not met.** "srt ACL provisioning proven end to end"
  stays open. Do not treat sandboxing as working until a probe passes.
- **Cost to phase 1: one step.** Audit step 6, first model load inside srt, is
  deferred. Benchmarking needs no sandbox, so nothing else slips.
- **Hard gate before phase 2.** The daemon must not be given real actions until
  this works, because srt is the only thing standing between an open-weight local
  model's tool calls and the filesystem. If it is still broken then, the options
  are a pinned older release, the Windows Sandbox container, or running the daemon
  inside WSL2 and accepting the loss of Windows access.

Re-test after any sandbox-runtime upgrade:

```powershell
& "C:\Users\<owner>\AppData\Roaming\npm\srt.cmd" `
    --settings "C:\Users\<owner>\jarvis\srt-settings.json" `
    -- cmd /c "echo ok> C:\Users\<owner>\jarvis\queue\srt-probe.txt"
```

## Setup still required

The Windows backend needs a one-time machine install that provisions the
`srt-sandbox` account, the `sandbox-runtime-users` group and the WFP filter set:

```powershell
npx @anthropic-ai/sandbox-runtime windows-install
```

It self-elevates with one UAC prompt and is idempotent, so re-running it rotates
the sandbox account password and reconciles the filters. Windows support is alpha.
Until this runs, no sandboxed invocation will work, and srt's own `initialize()` is
documented to fail with an actionable error rather than silently running unsandboxed.

## Private folders in `srt-settings.json`

The tracked policy denies reading `~/Documents/Work`, which is a placeholder for a folder of client
or employer work. If you keep such a folder under another name, change the two entries (`denyRead`
and the second list further down) in your own checkout and do not commit the real name; the same
goes for the matching line in `bin/phase0-elevated.ps1`.
