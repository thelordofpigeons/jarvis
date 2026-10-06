"""The hub's stylesheet and refresh script, served from /static/ so the page needs no inline code.

Why constants and not files: the package then has nothing to locate at run time, and the
Content-Security-Policy can say `style-src 'self'; script-src 'self'` with no exceptions.
No framework, no font download, no CDN: system fonts only.

Layer L3 (hub). No imports.
"""
from __future__ import annotations

CSS = """\
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
  --radius: 8px;
  --mono: ui-monospace, "Cascadia Mono", "Consolas", monospace;
  --sans: system-ui, "Segoe UI", sans-serif;
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
code, pre { font-family: var(--mono); font-size: 0.9em; }
code { background: var(--code-bg); padding: 0.1em 0.35em; border-radius: 4px; }
pre {
  background: var(--code-bg);
  padding: 0.9rem 1rem;
  border-radius: var(--radius);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
pre code { background: none; padding: 0; }
header.top {
  background: var(--panel);
  border-bottom: 1px solid var(--line);
  padding: 0.6rem 16px;
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
  gap: 0.5rem 1rem;
}
.brand { font-weight: 700; letter-spacing: 0.02em; }
nav { display: flex; flex-wrap: wrap; gap: 0.25rem; flex: 1 1 auto; }
nav a {
  color: var(--ink);
  text-decoration: none;
  padding: 0.3rem 0.7rem;
  border-radius: 999px;
  border: 1px solid transparent;
}
nav a:hover { border-color: var(--line); }
nav a[aria-current="page"] { background: var(--accent); color: var(--accent-ink); }
.pill {
  display: inline-block;
  padding: 0.1rem 0.6rem;
  border-radius: 999px;
  font-size: 0.85rem;
  border: 1px solid var(--line);
  color: var(--muted);
  white-space: nowrap;
}
.pill.ok { color: var(--ok); background: var(--ok-bg); border-color: transparent; }
.pill.warn { color: var(--warn); background: var(--warn-bg); border-color: transparent; }
.pill.bad { color: var(--bad); background: var(--bad-bg); border-color: transparent; }
main { max-width: 64rem; margin: 0 auto; padding: 1rem 16px 3rem; }
h1 { font-size: 1.5rem; margin: 0.4rem 0 0.8rem; }
h2 { font-size: 1.15rem; margin: 1.6rem 0 0.5rem; padding-bottom: 0.25rem; border-bottom: 1px solid var(--line); }
h3 { font-size: 1rem; margin: 1.1rem 0 0.3rem; }
p { margin: 0.5rem 0; }
.lede { color: var(--muted); margin-top: 0; }
.banner {
  border-radius: var(--radius);
  padding: 0.6rem 0.9rem;
  margin: 0 0 1rem;
  border: 1px solid transparent;
}
.banner.bad { background: var(--bad-bg); color: var(--bad); }
.banner.warn { background: var(--warn-bg); color: var(--warn); }
.banner.ok { background: var(--ok-bg); color: var(--ok); }
.actions { display: flex; flex-wrap: wrap; gap: 0.6rem 1rem; align-items: flex-start; margin-top: 0.8rem; }
.actions details { flex: 1 1 16rem; border: 1px solid var(--line); border-radius: var(--radius); padding: 0.4rem 0.7rem; }
.actions summary { cursor: pointer; }
form.stack { display: grid; gap: 0.5rem; margin-top: 0.5rem; }
form.stack label { display: grid; gap: 0.2rem; font-size: 0.9rem; color: var(--muted); }
input[type="text"], input[type="date"] {
  font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line);
  border-radius: 6px; padding: 0.35rem 0.5rem; width: 100%; box-sizing: border-box;
}
button {
  font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--line);
  border-radius: 6px; padding: 0.4rem 0.9rem; cursor: pointer;
}
button.primary { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
button:focus-visible, input:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 0.8rem 1rem;
  margin: 0.8rem 0;
}
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
  gap: 0.6rem 1rem;
  margin: 0;
}
.facts div { min-width: 0; }
.facts dt { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.ev td.detail { font-family: var(--mono); font-size: 0.82rem; color: var(--muted); overflow-wrap: anywhere; }
.digest ul, .digest ol { padding-left: 1.4rem; margin: 0.4rem 0; }
.digest li { margin: 0.2rem 0; overflow-wrap: anywhere; }
.digest h1 { font-size: 1.3rem; }
.empty { color: var(--muted); font-style: italic; }
footer {
  max-width: 64rem;
  margin: 0 auto;
  padding: 1rem 16px 2rem;
  color: var(--muted);
  font-size: 0.85rem;
}
@media (max-width: 40rem) {
  body { font-size: 15px; }
  h1 { font-size: 1.3rem; }
  .brand { width: 100%; }
  th, td { padding: 0.35rem 0.4rem; }
}
@media (prefers-reduced-motion: no-preference) {
  nav a { transition: border-color 0.15s ease; }
}
"""

JS = """\
// Re-fetches the current page and swaps in its <main>. Read-only: a GET of the same URL.
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
      })
      .catch(function () {
        if (stamp) { stamp.textContent = "refresh failed, showing the last good page"; }
      });
  }
  setInterval(refresh, seconds * 1000);
})();
"""
