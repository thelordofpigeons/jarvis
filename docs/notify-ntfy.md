# Phone push with a self-hosted ntfy on your tailnet

JARVIS can push the one-line digest notification to your phone through [ntfy](https://docs.ntfy.sh/),
next to (or instead of) the Windows toast. The server runs on a machine of yours inside your
Tailscale network, so the text never crosses a public service.

**Status, stated plainly.** The JARVIS side (`NtfyNotifier`, the toast fallback, the `multi`
adapter) is tested against a local fake server in `tests/test_notify_ntfy.py`. The server steps
below follow the ntfy and Tailscale documentation; they were written here, not replayed against
a live server by the test suite. Check the exact flags against `ntfy --help` and
`tailscale serve --help` for the versions you install, and run the test message in step 6.

## What is sent

The same text the toast shows: the digest status, two counts, a fixed phrase and the note
path, for example `Digest ready: 3 to look at, 2 held. brain/raw/jarvis/digest-2026-10-06.md`.
Never an item title, a commit subject, a PR title or any vault content. Alongside it ntfy
receives the title `JARVIS`, a priority, and a click URL built from the note path (default
`obsidian://open?vault=<vault folder>&file=<path without .md>`, which opens the note if the
vault is synced to the phone).

The server sees that text. That is why it is yours, on your tailnet.

## The six steps

### 1. Install the ntfy server on an always-on tailnet machine

Use the machine that stays on and is already in your tailnet (the PC that runs the daemon is
fine, so is a small server). Install from <https://docs.ntfy.sh/install/> for your system and
confirm with `ntfy --help`.

### 2. Configure it to listen on localhost only

In the server config (`server.yml`) keep ntfy off every public interface and deny anonymous
access, so only users you create can publish or read:

```yaml
base-url: "https://jarvis-host.example-tailnet.ts.net"
listen-http: "127.0.0.1:2586"
behind-proxy: true
auth-file: "<path to a writable user.db>"
auth-default-access: "deny-all"
```

Replace the `base-url` host with your machine's real tailnet name. Start the server.

### 3. Publish it on the tailnet with Tailscale Serve

Tailscale Serve puts HTTPS with a real certificate in front of the localhost port and is
reachable only from your tailnet. Check the syntax for your version with
`tailscale serve --help`; it looks like:

```
tailscale serve --bg 2586
```

Then `tailscale serve status` shows the HTTPS address. That address, without a trailing path, is
your `ntfy_url`. Do not use Tailscale Funnel: it would expose the server to the internet.

### 4. Create the two users and a token for JARVIS

One user may only publish, one may only read. The topic name is a shared secret of sorts, so
make it long and not guessable.

```
ntfy user add jarvis
ntfy user add phone
ntfy access jarvis <topic> write-only
ntfy access phone <topic> read-only
ntfy token add jarvis
```

The last command prints a token starting with `tk_`. Put it in a **user environment variable**
on the machine that runs the daemon, never in a file of this repo:

```
[Environment]::SetEnvironmentVariable('JARVIS_NTFY_TOKEN', '<the token>', 'User')
```

The scheduled task reads the user environment at logon, so sign out and in (or restart the
task) after setting it. Avoid pasting the token into a shell whose history you keep.

### 5. Point JARVIS at it in `jarvis.local.toml`

The address and topic are private, so they live in the gitignored local file:

```toml
[notify]
adapter = "ntfy"                          # "multi" sends the push and the toast
ntfy_url = "https://jarvis-host.example-tailnet.ts.net"
ntfy_topic = "<your long topic>"
ntfy_token_env = "JARVIS_NTFY_TOKEN"      # the NAME of the variable, never the token
ntfy_priority = 3                         # 1 (min) to 5 (max)
# ntfy_click_template = "obsidian://open?vault={vault}&file={path}"   # "" for no click action
```

`ntfy_url` must be `https://`, with one exception: plain `http://` is accepted only for a loopback
host (`localhost`, `127.x.x.x`, `[::1]`), because the bearer token and the digest line would
otherwise cross the network unencrypted. A configuration with `http://` and a LAN address is
refused at load time. The Tailscale Serve step above is what gives the tailnet address its
HTTPS certificate.

Restart the daemon (`JarvisDaemon`): the notifier is built once at start.

`adapter = "ntfy"` pushes first and shows the toast only if the push failed. `adapter = "multi"`
always does both.

### 6. Subscribe on the phone and send a test

Install the ntfy app, add your server (the HTTPS address from step 3) logged in as `phone`,
and subscribe to the topic. Then, from the repo:

```
.venv\Scripts\python.exe -c "from jarvisd.config import load_config; from jarvisd.notify import build_notifier; print(build_notifier(load_config()).send('JARVIS test. brain/raw/jarvis/test.md'))"
```

`NotifyResult(ok=True, detail='sent')` means the server answered with a 2xx. It does not prove
the phone showed it; that you see with your eyes.

## When it fails

The push never fails a digest. A failure becomes one audit record, `notify_ntfy_failed`, with a
short code only (`http_401`, `http_403`, `http_500`, `timeout`, `network`, `error`), never the
address, topic, token or text, and the toast is shown instead. Read the records with
`jarvis audit tail`. Common causes:

- `http_401` or `http_403`: wrong or expired token, or the `jarvis` user lacks write access to the topic.
- `timeout` or `network`: the server machine is off, or Tailscale is not running on this one.
- `http_302`: a proxy or Serve rule redirects. Redirects are refused on purpose so the token
  cannot be forwarded to another host; use the final address as `ntfy_url`.

## What it does not do

- No delivery receipt and no reply channel: JARVIS pushes, it does not read the topic.
- No proxy support: proxy environment variables are ignored because the server is private.
- iOS needs an upstream relay for instant delivery; that is outside this document and would
  send a hash of the topic to a public service. Android with the ntfy app (or the foreground
  service option) needs nothing extra.
