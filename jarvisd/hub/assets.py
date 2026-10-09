"""The hub's stylesheet, refresh script and preferences script, served from /static/ so the page needs no inline code.

Why constants and not files: the package then has nothing to locate at run time, and the
Content-Security-Policy can say `style-src 'self'; script-src 'self'` with no exceptions.
No framework, no font download, no CDN: the hub is a zero-network cockpit on a Windows machine, so the
typefaces are the ones Windows ships and nothing else (the CSS comment on `--sans` says which and why).

Layer L3 (hub). No imports.
"""
from __future__ import annotations

CSS = """\
/* Tokens. Colours, a five-step spacing scale and two radii; every rule below uses these and no literal. */
:root {
  --bg: #f7f7f5;
  --panel: #ffffff;
  --ink: #1d2327;
  --muted: #5c666e;
  --line: #dcdfe2;
  --accent: #1f5f8b;
  --accent-ink: #ffffff;
  --ok: #1d6b3a;
  --ok-bg: #e3f3e8;
  --warn: #8a5a00;
  --warn-bg: #fdf0d2;
  --bad: #9b1c1c;
  --bad-bg: #fbe3e3;
  --code-bg: #eef0f2;
  --space-1: 0.25rem;
  --space-2: 0.5rem;
  --space-3: 0.75rem;
  --space-4: 1rem;
  --space-5: 1.5rem;
  --radius-sm: 4px;
  --radius: 8px;
  --tap: 2.75rem;
  /* Local type, on purpose: nothing is fetched, so the page cannot leak a request and renders offline.
     Bahnschrift (Windows 10 and later) is a DIN-derived variable face with a 300 to 700 weight axis, which
     gives the light tile figures and the bold headings from one family; Segoe UI Variable is the fallback
     on the same machines, then the generic. Cascadia Mono carries code, ids and the raw CLI block. */
  --sans: "Bahnschrift", "Segoe UI Variable Text", "Segoe UI", sans-serif;
  --mono: "Cascadia Mono", ui-monospace, "Consolas", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14181b;
    --panel: #1c2226;
    --ink: #e6e9eb;
    --muted: #9aa5ad;
    --line: #2f383e;
    --accent: #6cb4e4;
    --accent-ink: #0d1114;
    --ok: #7fd49a;
    --ok-bg: #173323;
    --warn: #f0c36a;
    --warn-bg: #3a2d10;
    --bad: #f19a9a;
    --bad-bg: #3d1a1a;
    --code-bg: #262e33;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.5 var(--sans);
}
a { color: var(--accent); }
a:hover { color: var(--ink); }
a:focus-visible, nav a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
code, pre { font-family: var(--mono); font-size: 0.9em; }
code { background: var(--code-bg); padding: 0 var(--space-1); border-radius: var(--radius-sm); }
pre {
  background: var(--code-bg);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
pre code { background: none; padding: 0; }
/* Header: the status strip is the first row at every width, so the first screen answers "is it fine";
   brand and tabs share the second row. The health word in the strip is the one live health signal
   (aria-live sits on it); the avatar docks at the strip's right end. */
header.top {
  background: var(--panel);
  border-bottom: 1px solid var(--line);
  padding: var(--space-2) 16px var(--space-2);
  position: sticky;
  top: 0;
  z-index: 2;
}
.bar {
  max-width: 64rem;
  margin: 0 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-1) var(--space-4);
}
.strip {
  order: -1;
  flex: 1 1 100%;
  margin: 0;
  font-size: 0.9rem;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.strip b { color: var(--ink); font-weight: 700; }
.strip .short { display: none; }
.brand { font-weight: 700; letter-spacing: 0.02em; }
nav { display: flex; flex-wrap: wrap; gap: var(--space-1); flex: 1 1 auto; }
nav a {
  color: var(--ink);
  text-decoration: none;
  padding: var(--space-1) var(--space-3);
  border-radius: 999px;
  border: 1px solid transparent;
}
nav a:hover { border-color: var(--line); }
nav a[aria-current="page"] { background: var(--accent); color: var(--accent-ink); }
.pill {
  display: inline-block;
  padding: 0 var(--space-2);
  border-radius: 999px;
  font-size: 0.85rem;
  border: 1px solid var(--line);
  color: var(--muted);
  white-space: nowrap;
}
.pill.ok { color: var(--ok); background: var(--ok-bg); border-color: transparent; }
.pill.warn { color: var(--warn); background: var(--warn-bg); border-color: transparent; }
.pill.bad { color: var(--bad); background: var(--bad-bg); border-color: transparent; }
main { max-width: 64rem; margin: 0 auto; padding: var(--space-4) 16px 3rem; }
/* Type: two weights far apart (300 for the big figures, 700 for headings), sizes that step clearly. */
h1 { font-size: 1.9rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.15; margin: var(--space-2) 0 var(--space-3); }
h2 { font-size: 1.1rem; font-weight: 700; margin: var(--space-5) 0 var(--space-2); padding-bottom: var(--space-1); border-bottom: 1px solid var(--line); }
h3 { font-size: 1rem; font-weight: 700; margin: var(--space-4) 0 var(--space-1); }
h4 { font-size: 0.85rem; margin: var(--space-3) 0 var(--space-1); color: var(--muted); font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
p { margin: var(--space-2) 0; }
.lede { color: var(--muted); font-size: 0.95rem; margin-top: 0; }
.banner {
  border-radius: var(--radius);
  padding: var(--space-2) var(--space-3);
  margin: 0 0 var(--space-4);
  border: 1px solid transparent;
}
.banner.bad { background: var(--bad-bg); color: var(--bad); }
.banner.warn { background: var(--warn-bg); color: var(--warn); }
.banner.ok { background: var(--ok-bg); color: var(--ok); }
/* Inbox actions: one row of three same-height controls; the Edit and Reject forms open below the row. */
.actions { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: flex-start; margin-top: var(--space-3); }
.actions details { margin: 0; flex: 0 0 auto; }
.actions details[open] { flex: 1 1 100%; }
.actions details > summary {
  display: inline-block; list-style: none; font-weight: 400; border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--panel); padding: var(--space-2) var(--space-3); margin: 0; cursor: pointer;
}
.actions details > summary::-webkit-details-marker { display: none; }
.actions details > summary:hover { border-color: var(--accent); }
.actions details[open] > summary { border-color: var(--accent); margin-bottom: var(--space-2); }
form.stack { display: grid; gap: var(--space-2); margin-top: var(--space-2); }
form.stack label { display: grid; gap: var(--space-1); font-size: 0.9rem; color: var(--muted); }
form.inline { display: inline; }
input[type="text"], input[type="date"] {
  font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: var(--space-2) var(--space-2); width: 100%; box-sizing: border-box;
}
button {
  font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); cursor: pointer;
}
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
button.primary:hover { filter: brightness(1.08); }
button:focus-visible, input:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: var(--space-3) var(--space-4);
  margin: var(--space-3) 0;
}
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
  gap: var(--space-2) var(--space-4);
  margin: 0;
}
.facts div { min-width: 0; }
.facts dt { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
th, td { text-align: left; padding: var(--space-2) var(--space-2); border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
tbody tr:hover { background: var(--panel); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.ev td.detail { font-family: var(--mono); font-size: 0.82rem; color: var(--muted); overflow-wrap: anywhere; }
/* The raw note inside Full digest: its own headings are demoted one level so they do not read as page sections. */
.digest ul, .digest ol { padding-left: var(--space-5); margin: var(--space-1) 0; }
.digest li { margin: var(--space-1) 0; overflow-wrap: anywhere; }
.digest h1 { font-size: 1.3rem; }
.digest h2 { font-size: 1rem; border-bottom: 0; margin: var(--space-4) 0 var(--space-1); }
.digest h3 { font-size: 0.9rem; color: var(--muted); }
.empty { color: var(--muted); font-style: italic; }
/* Today: short rows, a numbered checklist, a visible end. Ids live in data attributes, never in the text. */
ul.rows, ul.raw, ul.waiting { list-style: none; padding: 0; margin: var(--space-1) 0; }
ul.rows li, ul.raw li, ul.waiting li {
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--line);
  overflow-wrap: anywhere;
}
ul.rows li:last-child, ul.raw li:last-child, ul.waiting li:last-child { border-bottom: 0; }
ul.raw li { color: var(--muted); font-size: 0.92rem; }
ol.needs { padding-left: 1.6rem; margin: var(--space-1) 0; }
ol.needs li { padding: var(--space-2) 0 var(--space-2) var(--space-1); border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
ol.needs li:last-child { border-bottom: 0; }
ol.needs li::marker { color: var(--accent); font-weight: 700; }
ul.waiting li { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1) var(--space-2); }
ul.waiting .title { flex: 1 1 14rem; }
.age { color: var(--muted); font-size: 0.85rem; white-space: nowrap; }
.calm { margin: var(--space-1) 0; }
.end { color: var(--muted); margin: 2rem 0 0; text-align: center; letter-spacing: 0.04em; }
details { margin: var(--space-2) 0; }
details > summary {
  cursor: pointer;
  font-weight: 700;
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--line);
  list-style-position: inside;
}
details > summary:hover { color: var(--accent); }
details[open] > summary { margin-bottom: var(--space-1); }
details > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
details.help > summary { font-weight: 400; color: var(--muted); border-bottom: 0; font-size: 0.9rem; }
/* Inbox cards: title, why, pills, buttons; the identifiers behind a disclosure. */
.proposal h2 { border-bottom: 0; margin: 0 0 var(--space-1); }
.proposal .why { margin: var(--space-1) 0 var(--space-1); }
.proposal .pills { margin: var(--space-1) 0; display: flex; flex-wrap: wrap; gap: var(--space-1); }
.proposal details.ids > summary { font-weight: 400; color: var(--muted); border-bottom: 0; }
.sort { color: var(--muted); }
/* Activity: a strip of tiles for the last 7 days; five per row from 64rem so ten tiles make two equal rows. */
dl.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(8.5rem, 1fr));
  gap: var(--space-2);
  margin: var(--space-2) 0 var(--space-4);
}
.tile {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: var(--space-2) var(--space-3);
  min-width: 0;
}
.tile dt { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.tile dd { margin: 0; font-size: 2rem; font-weight: 300; line-height: 1.2; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.tile.bad dd { color: var(--bad); }
/* Projects: the active table reads as a card list under 40rem (each cell carries its label). */
.quiet { margin-top: var(--space-4); }
footer {
  max-width: 64rem;
  margin: 0 auto;
  padding: var(--space-4) 16px 2rem;
  color: var(--muted);
  font-size: 0.85rem;
}
/* The avatar companion (views.page with face=True). A rail centred in the right margin while the margin
   holds it (viewport 88rem and up: 64rem of content plus two margins of 12rem), a 2.75rem dock at the
   right end of the status strip below that. Under 40rem the link keeps its 2.75rem hit box and the puppet
   shrinks to 1.75rem inside it. Fixed, so it never scrolls away and the page refresh (which swaps <main>
   only) never touches it. */
.companion {
  position: fixed;
  z-index: 3;
  top: 5.5rem;
  right: max(16px, calc((100vw - 64rem) / 4 - 4.5rem));
  width: 10rem;
  display: grid;
  justify-items: center;
  gap: var(--space-1);
}
.companion a { display: grid; place-items: center; width: 10rem; height: 10rem; border-radius: 50%; }
.companion a:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
.companion lantern-puppet { display: block; width: 100%; height: 100%; }
.companion .label {
  margin: 0;
  color: var(--muted);
  font-size: 0.8rem;
  letter-spacing: 0.06em;
  text-transform: lowercase;
  text-align: center;
}
@media (min-width: 64rem) {
  dl.tiles { grid-template-columns: repeat(5, 1fr); }
}
@media (max-width: 87.99rem) {
  .companion { top: var(--space-1); right: 16px; width: auto; }
  .companion a { width: 2.75rem; height: 2.75rem; }
  .companion .label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  body[data-face] .strip { padding-right: 3.25rem; }
}
@media (max-width: 40rem) {
  body { font-size: 15px; }
  h1 { font-size: 1.5rem; }
  .brand { width: 100%; }
  .strip { font-size: 0.85rem; }
  .companion { top: 0; }
  .companion lantern-puppet { width: 1.75rem; height: 1.75rem; }
  /* Touch: every control reaches 44 CSS px. Inline links that act (sort, waiting, note) carry `tap`. */
  nav a { padding: 0.55rem 0.9rem; min-height: var(--tap); display: inline-flex; align-items: center; }
  button, input[type="text"], input[type="date"], .actions details > summary { min-height: var(--tap); }
  .actions details > summary { display: inline-flex; align-items: center; }
  a.tap { display: inline-block; min-width: var(--tap); padding: var(--space-3) 0; }
  details > summary { padding: var(--space-3) 0; }
  .strip .long { display: none; }
  .strip .short { display: inline; }
  th, td { padding: var(--space-2) var(--space-1); }
  .cards table, .cards tbody, .cards tr, .cards td { display: block; }
  .cards thead { display: none; }
  .cards tr { border: 1px solid var(--line); border-radius: var(--radius); padding: var(--space-1) var(--space-2); margin: var(--space-2) 0; background: var(--panel); }
  .cards td { border-bottom: 0; padding: var(--space-1) 0; text-align: left; }
  .cards td::before { content: attr(data-label); display: block; font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .cards td:empty { display: none; }
  dl.tiles { grid-template-columns: repeat(2, 1fr); }
}
@media (prefers-reduced-motion: no-preference) {
  nav a, button, details > summary { transition: border-color 0.15s ease, color 0.15s ease; }
}
"""

JS = """\
// Re-fetches the current page and swaps in its <main>. Read-only: a GET of the same URL.
// After each swap it announces `hub:refreshed` on the document, so prefs.js can restore <details> state.
(function () {
  var seconds = Number(document.body.getAttribute("data-refresh")) || 0;
  var main = document.getElementById("main");
  var stamp = document.getElementById("stamp");
  if (!seconds || !main || !window.fetch || !window.DOMParser) { return; }
  function refresh() {
    if (document.hidden) { return; }
    fetch(location.pathname, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(new Error(String(r.status))); })
      .then(function (text) {
        var next = new DOMParser().parseFromString(text, "text/html").getElementById("main");
        if (next) { main.innerHTML = next.innerHTML; }
        if (stamp) { stamp.textContent = "refreshed " + new Date().toLocaleTimeString(); }
        document.dispatchEvent(new CustomEvent("hub:refreshed"));
      })
      .catch(function () {
        if (stamp) { stamp.textContent = "refresh failed, showing the last good page"; }
      });
  }
  setInterval(refresh, seconds * 1000);
})();
"""

PREFS_JS = """\
// Per-viewer conveniences only: which <details> are open, remembered per page in this browser's localStorage.
// Loaded on every page, with or without the refresh script; nothing here talks to the server.
(function () {
  var key = "hub:details:" + location.pathname;
  function load() {
    try { return JSON.parse(localStorage.getItem(key) || "{}") || {}; } catch (e) { return {}; }
  }
  function save(state) {
    try { localStorage.setItem(key, JSON.stringify(state)); } catch (e) { /* private window or storage off */ }
  }
  function restore() {
    var state = load();
    var all = document.querySelectorAll("details[id]");
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (Object.prototype.hasOwnProperty.call(state, el.id)) { el.open = !!state[el.id]; }
    }
  }
  document.addEventListener("toggle", function (ev) {
    var el = ev.target;
    if (!el || el.tagName !== "DETAILS" || !el.id) { return; }
    var state = load();
    state[el.id] = el.open;
    save(state);
  }, true);
  restore();
  document.addEventListener("hub:refreshed", restore);
})();
"""
