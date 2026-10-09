"""The /face page: the lantern avatar's home in the hub.

Three things live here: the page, its stylesheet and script (constants, like jarvisd/hub/assets.py, so the
Content-Security-Policy stays `script-src 'self'; style-src 'self'` with no exception), and the allowlist that
decides which files of the avatar folder ([hub].face_dir) may be served under /static/face/.

The avatar's own files (lantern.js, lantern-puppet.js, body/*.png) are read from disk, never written.

Layer L3 (hub). Imports nothing from the daemon.
"""
from __future__ import annotations

import re
from pathlib import Path

ASSET_NAME = re.compile(r"^(?:lantern\.js|lantern-puppet\.js|body/poses\.json|body/[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.png)$")
MEDIA_TYPES = {".js": "text/javascript; charset=utf-8", ".json": "application/json", ".png": "image/png"}
MAX_ASSET_BYTES = 8_000_000
REQUIRED = ("lantern.js", "lantern-puppet.js")

# One puppet, calibrated once: the default body and where its face sits (lantern-avatar/body/f01.png).
PUPPET_TAG = ('<lantern-puppet id="avatar" src="/static/face/body/f01.png" face-x="514" face-y="470" radius="300" '
              'poses="/static/face/body/poses.json"></lantern-puppet>')
SCRIPT_TAGS = ('<script type="module" src="/static/face/lantern-puppet.js"></script>'
               '<script type="module" src="/static/face.js"></script>')
# The companion on every hub view: a rail in the right margin on a wide screen, a dock in the header
# below that (hub.css .companion). Outside <main>, so the page refresh never swaps it out. The link
# opens /face, the avatar's own page, for a second window or a phone.
COMPANION = ('<aside class="companion" aria-label="JARVIS face"><a href="/face" title="Open the face on its own">'
             + PUPPET_TAG + '</a><p id="state" class="label" role="status" aria-live="polite">idle</p></aside>')


def installed(face_dir: Path) -> bool:
    try:
        return all((face_dir / name).is_file() for name in REQUIRED)
    except OSError:
        return False


def resolve_asset(face_dir: Path, name: str) -> Path | None:
    """The file behind a /static/face/<name> request, or None.

    The name must match the allowlist before the disk is touched (no `..`, no separators beyond `body/`, no
    drive letters or stream suffixes). The resolved path must then still sit inside face_dir, so a symlink or
    junction that points elsewhere is refused too."""
    if not ASSET_NAME.fullmatch(name):
        return None
    try:
        root = face_dir.resolve()
        path = (face_dir / name).resolve()
        if not path.is_file() or not path.is_relative_to(root) or path.stat().st_size > MAX_ASSET_BYTES:
            return None
    except (OSError, ValueError):
        return None
    return path


def media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


PAGE = f"""\
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS face</title>
<link rel="stylesheet" href="/static/face.css"></head>
<body>
<main class="stage">
{PUPPET_TAG}
<p id="state" class="label" role="status" aria-live="polite">idle</p>
</main>
{SCRIPT_TAGS}
</body></html>
"""

NOT_INSTALLED = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS face</title>
<link rel="stylesheet" href="/static/face.css"></head>
<body>
<main class="stage"><p class="label">The avatar assets are not installed. Set [hub].face_dir in jarvis.local.toml
to the folder that holds lantern.js and lantern-puppet.js. See docs/hub.md.</p></main>
</body></html>
"""

CSS = """\
:root {
  --bg: #15110f;
  --ink: #f1e6d6;
  --muted: #a89a88;
  --sans: system-ui, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.4 var(--sans);
}
.stage {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 16px;
}
lantern-puppet {
  --lantern-size: min(80vw, 72vh);
  display: block;
  width: var(--lantern-size);
  height: var(--lantern-size);
}
.label {
  margin: 0;
  color: var(--muted);
  font-size: 0.85rem;
  letter-spacing: 0.06em;
  text-transform: lowercase;
  text-align: center;
  max-width: 32rem;
}
"""

JS = """\
// The /face page. Polls /api/status every 5 s, maps it to an avatar state, and never writes anything.
// mapState() is pure so tests/test_hub_face.py can run it under node; the DOM part only runs in a browser.
export const POLL_MS = 5000;
export const STALE_MS = 30 * 60 * 1000;
export const HAPPY_MS = 2000;
export const NIGHT_FROM = 23;
export const NIGHT_TO = 7;

export function isNight(date) {
  const h = date.getHours();
  return h >= NIGHT_FROM || h < NIGHT_TO;
}

function signature(status) {
  const face = status.face || {};
  const last = face.last_finished || {};
  return JSON.stringify([status.running, status.kill, !!status.pause, (status.breaker || {}).state,
    (status.queue || {}).running, face.inbox_pending, last.id, last.state, last.at]);
}

// memory: { sig, changedAt, lastAt } carried between polls. Returns { state, react } where react is "happy"
// when a run finished since the previous poll.
export function mapState(status, now, memory) {
  const face = status.face || {};
  const last = face.last_finished || null;
  const sig = signature(status);
  const first = memory.sig === undefined;
  if (first || sig !== memory.sig) { memory.changedAt = now.getTime(); }
  memory.sig = sig;
  const finishedNow = !first && last && last.state === "done" && last.at !== memory.lastAt;
  memory.lastAt = last ? last.at : null;
  const react = finishedNow ? "happy" : null;

  const breaker = (status.breaker || {}).state;
  const disabled = status.kill || status.pause || (breaker && breaker !== "closed");
  const running = Number((status.queue || {}).running || 0) > 0;
  const stale = now.getTime() - memory.changedAt >= STALE_MS;
  const night = isNight(now);

  let state;
  if (disabled) { state = "sad"; }
  else if (running) { state = "thinking"; }
  else if (last && last.state === "failed") { state = "sad"; }
  else if (!status.running) { state = "sleepy"; }
  else if (Number(face.inbox_pending || 0) > 0) { state = night ? "sleepy" : "listening"; }
  else { state = (night || stale) ? "sleepy" : "idle"; }
  return { state, react };
}

function start() {
  const el = document.getElementById("avatar");
  const label = document.getElementById("state");
  if (!el) { return; }
  const memory = {};
  let shown = "";
  function show(state, text) {
    if (state !== shown) { shown = state; el.state = state; }
    if (label) { label.textContent = text || state; }
  }
  function poll() {
    fetch("/api/status", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(String(r.status))); })
      .then(function (status) {
        const out = mapState(status, new Date(), memory);
        show(out.state);
        if (out.react && typeof el.react === "function") { el.react(out.react, HAPPY_MS); }
      })
      .catch(function () { show("confused", "hub unreachable"); });
  }
  poll();
  setInterval(poll, POLL_MS);
}

if (typeof document !== "undefined") { start(); }
"""
