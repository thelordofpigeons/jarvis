"""The /face page: routes, the static allowlist, the security boundaries it must keep, and the state mapping.

The avatar folder is synthetic (a few bytes per file); the mapping is run under node when node is installed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvisd import ROOT  # noqa: E402
from jarvisd.config import Config  # noqa: E402
from jarvisd.hub import app as hub_app  # noqa: E402
from jarvisd.hub import face  # noqa: E402

HOST = "http://127.0.0.1:8765"
PNG = b"\x89PNG\r\n\x1a\nsynthetic"


@pytest.fixture
def face_cfg(tmp_cfg: Config, tmp_path: Path) -> Config:
    folder = tmp_path / "avatar"
    (folder / "body").mkdir(parents=True)
    (folder / "lantern.js").write_text("export const STATES = [];\n", encoding="utf-8")
    (folder / "lantern-puppet.js").write_text("import './lantern.js';\n", encoding="utf-8")
    (folder / "body" / "f01.png").write_bytes(PNG)
    (folder / "body" / "poses.json").write_text('{"poses": ["f01.png"]}', encoding="utf-8")
    (folder / "secret.txt").write_text("not for the web", encoding="utf-8")
    (tmp_path / "outside.png").write_bytes(PNG)
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.hub.face_dir = folder
    return cfg


def _client(cfg: Config, host: str = HOST) -> TestClient:
    return TestClient(hub_app.create_app(cfg), base_url=host)


def test_every_hub_view_carries_the_face_companion_when_the_assets_exist(face_cfg: Config, tmp_path: Path) -> None:
    body = _client(face_cfg).get("/status").text
    assert 'data-face="1"' in body and '<aside class="companion"' in body and 'id="avatar"' in body
    assert 'href="/face"' in body and 'id="state"' in body
    assert face.SCRIPT_TAGS in body and '<script src="/static/hub.js">' in body
    # outside <main>, so the refresh script (which swaps <main> only) leaves the avatar alone
    assert body.index("</header>") < body.index('<aside class="companion"') < body.index('<main id="main">')
    assert not re.search(r"<script(?![^>]*\bsrc=)", body) and "style=" not in body.split("<main")[0]
    without = face_cfg.model_copy(deep=True)
    without.hub.face_dir = tmp_path / "nowhere"
    plain = _client(without).get("/status").text
    assert "companion" not in plain and "lantern" not in plain and "data-face" not in plain


def test_face_page_has_the_puppet_and_only_external_code(face_cfg: Config) -> None:
    resp = _client(face_cfg).get("/face")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    assert "<lantern-puppet" in resp.text and 'face-x="514"' in resp.text and 'id="state"' in resp.text
    assert '<script type="module" src="/static/face.js">' in resp.text
    assert "style=" not in resp.text and not re.search(r"<script(?![^>]*\bsrc=)", resp.text)
    assert "<style" not in resp.text and not re.search(r"https?://", resp.text)
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "default-src 'none'" in csp


def test_face_assets_are_served_with_types(face_cfg: Config) -> None:
    client = _client(face_cfg)
    css, js = client.get("/static/face.css"), client.get("/static/face.js")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "/api/status" in js.text
    for text in (css.text, js.text):
        assert not re.search(r"https?://", text)
    assert "transition: all" not in css.text and not re.search(r"#[0-9a-fA-F]{3,8}\b", css.text.split("}", 1)[1])
    lantern = client.get("/static/face/lantern.js")
    assert lantern.status_code == 200 and "javascript" in lantern.headers["content-type"]
    assert client.get("/static/face/lantern-puppet.js").status_code == 200
    png = client.get("/static/face/body/f01.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content == PNG
    poses = client.get("/static/face/body/poses.json")
    assert poses.status_code == 200 and poses.json()["poses"] == ["f01.png"]


@pytest.mark.parametrize("name", [
    "secret.txt", "body/secret.txt", "body/missing.png", "body/", "body", "../outside.png", "body/../secret.txt",
    "body/../../outside.png", "%2e%2e/outside.png", "..%2foutside.png", "body/..%2f..%2foutside.png",
    "body%5c..%5c..%5coutside.png", "body\\f01.png", "body/f01.png:stream", "/etc/passwd", "C:/Windows/win.ini",
    "body/sub/f01.png", "lantern.js/", "LANTERN.JS.bak", "body/.hidden.png", "body/f01.PNG.exe",
])
def test_static_allowlist_refuses_everything_else(face_cfg: Config, name: str) -> None:
    resp = _client(face_cfg).get(f"/static/face/{name}")
    assert resp.status_code == 404, name
    assert b"not for the web" not in resp.content


def test_a_symlink_out_of_the_folder_is_refused(face_cfg: Config, tmp_path: Path) -> None:
    link = face_cfg.hub.face_dir / "body" / "link.png"
    try:
        link.symlink_to(tmp_path / "outside.png")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need a privilege on this machine")
    assert _client(face_cfg).get("/static/face/body/link.png").status_code == 404


def test_resolve_asset_checks_the_name_before_the_disk(face_cfg: Config) -> None:
    folder = face_cfg.hub.face_dir
    assert face.resolve_asset(folder, "body/f01.png") == (folder / "body" / "f01.png").resolve()
    assert face.resolve_asset(folder, "../avatar/lantern.js") is None
    assert face.resolve_asset(folder / "nope", "lantern.js") is None


def test_host_guard_applies_to_the_face_routes(face_cfg: Config) -> None:
    app = hub_app.create_app(face_cfg)
    for path in ("/face", "/static/face.js", "/static/face.css", "/static/face/lantern.js",
                 "/static/face/body/f01.png"):
        assert TestClient(app, base_url="http://127.0.0.1:8765").get(path).status_code == 200, path
        assert TestClient(app, base_url="http://localhost:8765").get(path).status_code == 200, path
        assert TestClient(app, base_url="http://evil.example:8765").get(path).status_code == 403, path


def test_face_routes_are_read_only(face_cfg: Config) -> None:
    client = _client(face_cfg)
    for method in ("post", "put", "delete", "patch"):
        for path in ("/face", "/static/face.js", "/static/face/lantern.js"):
            assert getattr(client, method)(path).status_code == 405, (method, path)


def test_face_responses_carry_the_security_headers(face_cfg: Config) -> None:
    client = _client(face_cfg)
    for path in ("/face", "/static/face.css", "/static/face.js", "/static/face/body/f01.png"):
        resp = client.get(path)
        assert resp.headers["x-frame-options"] == "DENY" and resp.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


def test_a_missing_face_dir_gives_the_not_installed_page(tmp_cfg: Config, tmp_path: Path) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.hub.face_dir = tmp_path / "no-such-folder"
    client = _client(cfg)
    resp = client.get("/face")
    assert resp.status_code == 200 and "not installed" in resp.text and "<lantern-puppet" not in resp.text
    assert client.get("/static/face/lantern.js").status_code == 404
    assert not cfg.hub.face_dir.exists()


def test_a_folder_without_the_scripts_is_not_installed(face_cfg: Config) -> None:
    (face_cfg.hub.face_dir / "lantern-puppet.js").unlink()
    assert "not installed" in _client(face_cfg).get("/face").text


def test_the_face_page_never_writes(face_cfg: Config) -> None:
    before = sorted(p.name for p in face_cfg.hub.face_dir.rglob("*"))
    client = _client(face_cfg)
    for path in ("/face", "/static/face.js", "/static/face/body/f01.png", "/static/face/../x"):
        client.get(path)
    assert sorted(p.name for p in face_cfg.hub.face_dir.rglob("*")) == before


def test_status_json_carries_the_face_signal(face_cfg: Config) -> None:
    got = _client(face_cfg).get("/api/status").json()
    assert got["face"] == {"inbox_pending": 0, "last_finished": None}


def test_face_dir_default_and_tracked_config() -> None:
    import tomllib

    from jarvisd.config import HubCfg

    assert HubCfg().face_dir == Path.home() / "lantern-avatar"
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    assert "host" not in raw["hub"] and "face_dir" not in raw["hub"] or isinstance(raw["hub"]["face_dir"], str)


def test_launcher_opens_an_app_window_on_the_configured_port() -> None:
    text = (ROOT / "bin" / "face-window.cmd").read_text(encoding="utf-8")
    assert "--app=" in text and "--window-size=420,460" in text and "msedge" in text and "chrome" in text
    assert "127.0.0.1" in text and "/face" in text


def test_hub_launcher_starts_the_hub_then_opens_an_app_window() -> None:
    text = (ROOT / "bin" / "hub.cmd").read_text(encoding="utf-8")
    assert "--app=" in text and "msedge" in text and "chrome" in text and "127.0.0.1" in text
    assert "JarvisHub" in text and "-m jarvisd hub" in text and "LISTENING" in text
    shortcuts = (ROOT / "deploy" / "make-shortcuts.ps1").read_text(encoding="utf-8")
    assert "hub.cmd" in shortcuts and "face-window.cmd" in shortcuts and "jarvis.ico" in shortcuts
    assert (ROOT / "bin" / "jarvis.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"


# --- the state mapping, run under node ---------------------------------------------------------------


NODE = shutil.which("node")

HARNESS = """
import { mapState, STALE_MS } from "%s";
const base = () => ({ running: true, kill: false, pause: null, breaker: { state: "closed" },
  queue: { pending: 0, running: 0, done: 1, failed: 0, held: 0 },
  face: { inbox_pending: 0, last_finished: { id: "j1", state: "done", at: "2026-10-06T10:00:00+00:00" } } });
const noon = new Date(2026, 9, 6, 12, 0, 0);
const out = {};
const run = (name, mutate, when = noon, memory = {}) => {
  const s = base(); mutate(s); out[name] = mapState(s, when, memory).state; return memory;
};
run("idle", () => {});
run("thinking", s => { s.queue.running = 1; });
run("sad_failed", s => { s.face.last_finished.state = "failed"; });
run("sad_kill", s => { s.kill = true; });
run("sad_pause", s => { s.pause = { reason: "x" }; });
run("sad_breaker", s => { s.breaker.state = "open"; });
run("listening", s => { s.face.inbox_pending = 2; });
run("sleepy_stopped", s => { s.running = false; });
run("sleepy_night", () => {}, new Date(2026, 9, 6, 23, 30, 0));
run("sleepy_early", () => {}, new Date(2026, 9, 6, 6, 59, 0));
run("awake_7", () => {}, new Date(2026, 9, 6, 7, 0, 0));
run("night_listening", s => { s.face.inbox_pending = 1; }, new Date(2026, 9, 6, 2, 0, 0));
run("thinking_at_night", s => { s.queue.running = 1; }, new Date(2026, 9, 6, 2, 0, 0));
const mem = run("fresh", () => {});
out.stale = mapState(base(), new Date(noon.getTime() + STALE_MS + 1000), mem).state;
const mem2 = {};
out.first_react = mapState(base(), noon, mem2).react;
const next = base(); next.face.last_finished = { id: "j2", state: "done", at: "2026-10-06T10:05:00+00:00" };
out.react_on_new_done = mapState(next, new Date(noon.getTime() + 5000), mem2).react;
out.react_once = mapState(next, new Date(noon.getTime() + 10000), mem2).react;
const failed = base(); failed.face.last_finished = { id: "j3", state: "failed", at: "2026-10-06T10:06:00+00:00" };
out.no_react_on_failure = mapState(failed, new Date(noon.getTime() + 15000), mem2).react;
const busy = base(); busy.face.inbox_pending = 1;
const mem3 = {}; mapState(busy, noon, mem3);
out.listening_stays = mapState(busy, new Date(noon.getTime() + STALE_MS * 2), mem3).state;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_state_mapping(tmp_path: Path) -> None:
    module = tmp_path / "face.mjs"
    module.write_text(face.JS, encoding="utf-8")
    runner = tmp_path / "run.mjs"
    runner.write_text(HARNESS % module.as_uri(), encoding="utf-8")
    done = subprocess.run([NODE, str(runner)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got == {
        "idle": "idle", "thinking": "thinking", "sad_failed": "sad", "sad_kill": "sad", "sad_pause": "sad",
        "sad_breaker": "sad", "listening": "listening", "sleepy_stopped": "sleepy", "sleepy_night": "sleepy",
        "sleepy_early": "sleepy", "awake_7": "idle", "night_listening": "sleepy", "thinking_at_night": "thinking",
        "fresh": "idle", "stale": "sleepy", "first_react": None, "react_on_new_done": "happy", "react_once": None,
        "no_react_on_failure": None, "listening_stays": "listening",
    }


# --- phase 2: the status strip and the preferences script ----------------------------------------------------------


def test_every_view_loads_prefs_js_and_the_strip_sits_in_the_header(face_cfg: Config) -> None:
    body = _client(face_cfg).get("/activity").text
    assert '<script src="/static/prefs.js"></script>' in body
    header = body.split("</header>")[0]
    assert '<p class="strip" id="strip" role="status">' in header and "<b " in header
    assert '<b aria-live="polite">' in header  # the strip's health word is the live region
    assert 'aria-label="JARVIS face, open"' in body and '<lantern-puppet aria-hidden="true"' in body
    assert body.index("</header>") < body.index('<aside class="companion"') < body.index('<main id="main">')
    prefs = _client(face_cfg).get("/static/prefs.js")
    assert prefs.status_code == 200 and "localStorage" in prefs.text and "hub:refreshed" in prefs.text
    assert not re.search(r"https?://", prefs.text)
    assert "stripText" in face.JS and 'getElementById("strip")' in face.JS


def test_the_companion_css_keeps_the_pill_visible_above_phone_width() -> None:
    from jarvisd.hub.assets import CSS

    assert "body[data-face] .bar .pill { display: none; }" not in CSS
    wide = CSS.split("@media (max-width: 87.99rem)")[1].split("@media (max-width: 40rem)")[0]
    assert ".pill" not in wide  # the dock no longer hides the health pill between 40rem and 88rem
    assert "body[data-face] .strip { padding-right: 3.25rem; }" in wide
    phone = CSS.split("@media (max-width: 40rem)")[1]
    assert ".companion lantern-puppet { width: 1.75rem; height: 1.75rem; }" in phone  # inside the 2.75rem link


STRIP_HARNESS = """
import { stripText } from "%s";
const now = new Date(2026, 9, 6, 12, 0, 0);
const base = () => ({ running: true, kill: false, pause: null, breaker: { state: "closed" },
  queue: { pending: 0, running: 0, done: 1, failed: 2 }, next_due: new Date(2026, 9, 7, 6, 30).toISOString(),
  last_digest: { at: new Date(2026, 9, 6, 6, 31).toISOString(), status: "complete" },
  face: { inbox_pending: 5, last_finished: null } });
const out = {};
const plain = stripText(base(), now);
out.plain = { health: plain.health, rest: plain.rest };
out.short = plain.short;
const failed = base(); failed.last_digest.status = "failed"; out.failed = stripText(failed, now).rest;
out.failedShort = stripText(failed, now).short;
const none = base(); none.last_digest = null; out.none = stripText(none, now).rest;
const kill = base(); kill.kill = true; out.kill = stripText(kill, now).health;
const pause = base(); pause.pause = { reason: "x" }; out.pause = stripText(pause, now).health;
const open = base(); open.breaker.state = "open"; out.open = stripText(open, now).health;
const stopped = base(); stopped.running = false; out.stopped = stripText(stopped, now).health;
const today = base(); today.next_due = new Date(2026, 9, 6, 18, 0).toISOString(); out.today = stripText(today, now).rest;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_strip_text_matches_the_server_words(tmp_path: Path) -> None:
    module = tmp_path / "face.mjs"
    module.write_text(face.JS, encoding="utf-8")
    runner = tmp_path / "strip.mjs"
    runner.write_text(STRIP_HARNESS % module.as_uri(), encoding="utf-8")
    done = subprocess.run([NODE, str(runner)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["plain"] == {"health": "Running.", "rest": "Last digest 06:31. Next 06:30 tomorrow. 5 waiting. 2 failed."}
    assert got["short"] == "Next 06:30 tomorrow. 5 waiting. 2 failed."  # the phone line drops a good last digest
    assert got["failed"].startswith("Last digest 06:31, failed.")
    assert got["failedShort"].startswith("Last digest 06:31, failed.")  # and keeps a failed one
    assert got["none"].startswith("No digest yet.")
    assert (got["kill"], got["pause"], got["open"], got["stopped"]) == ("Kill switch on.", "Paused.", "Claude calls paused.", "Stopped.")
    assert "Next 18:00 today." in got["today"]
