"""The FastAPI application: four GET views and the face, three static files, a JSON twin of Status and the Inbox's
three POST routes.

Boundaries, all enforced here and tested:
- Loopback only. `serve` passes host 127.0.0.1 and there is no host setting to change it.
  `tailscale serve` proxies a tailnet address to that loopback port.
- A Host header that is not loopback or listed in [hub].allowed_hosts gets 403. That stops a
  web page on another origin from reading the hub through DNS rebinding.
- /face serves an allowlisted set of files from [hub].face_dir (jarvisd/hub/face.py); nothing else of that folder.
- Everything is GET except POST /inbox/{id}/confirm, /edit and /reject; any other method gets 405 from the
  router. Those three read a small urlencoded body, and only after three checks: the Host guard above, an
  Origin that is absent or names the very host (and port) the request arrived on, and the per-process CSRF token
  (secrets.token_urlsafe, made when the app is built, sent in a hidden field, compared in constant time). The Host
  header was already checked against the allowlist, so "Origin equals Host" lets http://localhost:<port> and a
  `tailscale serve` name decide, and still refuses any other site. A GET never changes anything.
- The old routes (/runs, /ledger, /held, /audit, /status, /repos, /reminders) answer 301 to the view that absorbed
  them, query string dropped, so bookmarks survive and nothing else is served there.
- No CDN: the interactive API docs are switched off, CSS and JS are served from /static/.
- The hub never calls Claude. Its data layer (jarvisd/hub/data.py) only reads; the only writes are the Inbox
  decisions, made by jarvisd/inbox.py, the same functions `jarvis proposals confirm|reject` uses.

Layer L3 (hub).
"""
from __future__ import annotations

import hmac
import json
import secrets
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvisd import inbox
from jarvisd.config import Config
from jarvisd.hub import inbox as inbox_view
from jarvisd.hub import face, views
from jarvisd.hub.assets import CSS, JS, PREFS_JS
from jarvisd.hub.data import HubData

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
                                "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
MAX_FORM_BYTES = 16_384
# Outcome code of a refused decision to the HTTP status of the page that says why.
DECISION_STATUS = {"not_found": 404, "decided": 409, "busy": 409, "invalid": 422, "not_ready": 502,
                   "flagged": 422, "held": 422, "tracker_failed": 502, "maybe_created": 502, "unresolved": 409,
                   "save_failed": 500}
MAX_FORM_FIELDS = 20
# Where a decision may send the browser afterwards: the Inbox, or Today (its inline Confirm). Nothing else.
NEXT_PAGES = frozenset({"/", "/inbox"})


def host_name(header: str) -> str:
    """The host part of a Host header, lower case, port removed, IPv6 brackets kept."""
    text = header.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[: end + 1] if end != -1 else text
    return text.rsplit(":", 1)[0] if ":" in text else text


def origin_allowed(origin: str | None, host_header: str) -> bool:
    """True for no Origin, or an http(s) origin with no path whose host and port are the Host header's.

    The Host header has passed the allowlist before this runs. A page on another site sends its own
    origin, which is not the Host the request reached; "null" and anything with a path never match.
    https is only taken for a non-loopback name: nothing serves TLS on the loopback port itself.
    """
    if origin is None:
        return True
    try:
        parts = urlsplit(origin)
        parts.port  # noqa: B018  raises ValueError on a port that is not a number
    except ValueError:
        return False
    host = host_header.strip().lower()
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query or parts.fragment:
        return False
    if parts.username is not None or parts.password is not None or parts.netloc.lower() != host:
        return False
    return not (parts.scheme == "https" and host_name(host) in LOOPBACK_NAMES)


def create_app(cfg: Config, *, clock: Callable[[], datetime] | None = None, port: int | None = None,
               tracker: Any = None) -> FastAPI:
    """`port` is the one the server listens on (kept for the callers; the Origin check reads the Host header,
    which carries it); `tracker` replaces the configured adapter, which tests use and nothing else does."""
    data = HubData(cfg, clock)
    allowed = LOOPBACK_NAMES | {h.lower() for h in cfg.hub.allowed_hosts}
    refresh = cfg.hub.refresh_s
    csrf_token = secrets.token_urlsafe(32)  # per process: a page served by an earlier process cannot post to this one
    app = FastAPI(title="JARVIS hub", docs_url=None, redoc_url=None, openapi_url=None)

    def render(title: str, active: str, content: str, code: int = 200, *, live: bool = True,
               status: dict[str, Any] | None = None) -> HTMLResponse:
        # live=False drops the auto refresh: a re-fetch of the Inbox would wipe a half-typed edit.
        status = status if status is not None else data.status()
        return HTMLResponse(views.page(title, active, content, flags=status, refresh_s=refresh if live else 0,
                                       face=face.installed(cfg.hub.face_dir), strip=data.strip(status)),
                            status_code=code)

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[..., Any]) -> Response:
        if host_name(request.headers.get("host", "")) not in allowed:
            response: Response = PlainTextResponse("Host not allowed.\n", status_code=403)
        else:
            response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        return response

    @app.exception_handler(StarletteHTTPException)
    async def problem(request: Request, exc: StarletteHTTPException) -> Response:
        message = "Not found." if exc.status_code == 404 else "Method not allowed: the hub is read-only."
        return render("Error", "", views.not_found(exc.status_code, message), exc.status_code)

    def flash_for(request: Request) -> str:
        # The query only picks which banner to show; the banner text comes from the stored proposal.
        code, pid = request.query_params.get("ok", ""), request.query_params.get("id", "")
        if code in inbox_view.FLASH and inbox.valid_id(pid):
            return inbox_view.flash_html(next((p for p in inbox_view.all_proposals(data) if p.id == pid), None), code)
        return ""

    @app.get("/", response_class=HTMLResponse)
    def today_view(request: Request) -> HTMLResponse:
        return render("Today", "Today", views.today(data.today(), csrf_token, data.now().date(), flash=flash_for(request)))

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(request: Request) -> HTMLResponse:
        sort = request.query_params.get("sort", "newest")
        return render("Inbox", "Inbox", inbox_view.view(data, csrf_token, flash=flash_for(request), sort=sort), live=False)

    @app.get("/projects", response_class=HTMLResponse)
    def projects_view() -> HTMLResponse:
        return render("Projects", "Projects", views.projects(data.projects_page()))

    @app.get("/activity", response_class=HTMLResponse)
    def activity_view() -> HTMLResponse:
        model = data.activity()
        return render("Activity", "Activity", views.activity(model), status=model["status"])

    @app.get("/digest/{job_id}", response_class=HTMLResponse)
    def digest_view(job_id: str) -> HTMLResponse:
        note = data.digest_by_id(job_id)
        if note is None:
            return render("Not found", "", views.not_found(404, "No digest note with that id."), 404)
        return render(f"Digest {job_id}", "Activity", views.digest_page(note))

    def moved(target: str) -> Callable[[], Response]:
        def redirect() -> Response:
            return RedirectResponse(target, status_code=301)
        return redirect

    for old, target in views.REDIRECTS.items():
        app.add_api_route(old, moved(target), methods=["GET"], name=f"moved_{old.strip('/')}")

    async def form_fields(request: Request) -> dict[str, str] | Response:
        """The urlencoded body as a dict, or the refusal to send back."""
        if not origin_allowed(request.headers.get("origin"), request.headers.get("host", "")):
            return PlainTextResponse("Origin not allowed. Reload the Inbox from the address you used to open it "
                                     "and try again.\n", status_code=403)
        ctype = request.headers.get("content-type", "").lower()
        if ctype and not ctype.startswith("application/x-www-form-urlencoded"):
            return PlainTextResponse("Expected a form post.\n", status_code=415)
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = MAX_FORM_BYTES + 1
        if declared > MAX_FORM_BYTES:
            return PlainTextResponse("Form too large.\n", status_code=413)
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_FORM_BYTES:
                return PlainTextResponse("Form too large.\n", status_code=413)
        try:
            parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS)
        except ValueError:  # more fields than the forms have: refuse before the token check, without a traceback
            return PlainTextResponse("Malformed form.\n", status_code=400)
        fields = {k: v[0] for k, v in parsed.items() if v}
        if not hmac.compare_digest(fields.get("csrf", "").encode("utf-8"), csrf_token.encode("utf-8")):
            return PlainTextResponse("Missing or wrong CSRF token. Reload the Inbox and try again.\n", status_code=403)
        if "next" in fields and fields["next"] not in NEXT_PAGES:
            return PlainTextResponse("Unknown page to return to.\n", status_code=400)
        return fields

    async def decide(request: Request, proposal_id: str, action: str) -> Response:
        fields = await form_fields(request)
        if isinstance(fields, Response):
            return fields
        audit = inbox.audit_for(cfg, clock)
        if action == "reject":
            result = await run_in_threadpool(inbox.reject, cfg, audit, proposal_id, fields.get("reason"))
        else:
            edit = action == "edit"
            result = await run_in_threadpool(
                lambda: inbox.confirm(cfg, audit, tracker, proposal_id, clock=clock,
                                      override=fields.get("override") == "1",
                                      title=fields.get("title") if edit else None,
                                      project=fields.get("project") if edit else None,
                                      due=fields.get("due") if edit else None))
        if result.ok:
            back = fields.get("next", "/inbox")
            return RedirectResponse(f"{back}?ok={result.code}&id={proposal_id}", status_code=303)
        content = inbox_view.view(data, csrf_token, error=result.message)
        return render("Inbox", "Inbox", content, DECISION_STATUS.get(result.code, 500), live=False)

    @app.post("/inbox/{proposal_id}/confirm")
    async def inbox_confirm(request: Request, proposal_id: str) -> Response:
        return await decide(request, proposal_id, "confirm")

    @app.post("/inbox/{proposal_id}/edit")
    async def inbox_edit(request: Request, proposal_id: str) -> Response:
        return await decide(request, proposal_id, "edit")

    @app.post("/inbox/{proposal_id}/reject")
    async def inbox_reject(request: Request, proposal_id: str) -> Response:
        return await decide(request, proposal_id, "reject")

    @app.get("/api/status")
    def status_json() -> Response:
        return Response(json.dumps(data.status(), indent=2, ensure_ascii=False, default=str),
                        media_type="application/json")

    @app.get("/face", response_class=HTMLResponse)
    def face_page() -> HTMLResponse:
        # Not installed is a page, not a 500: the avatar folder is optional.
        return HTMLResponse(face.PAGE if face.installed(cfg.hub.face_dir) else face.NOT_INSTALLED)

    @app.get("/static/face.css")
    def face_css() -> Response:
        return Response(face.CSS, media_type="text/css; charset=utf-8")

    @app.get("/static/face.js")
    def face_js() -> Response:
        return Response(face.JS, media_type="text/javascript; charset=utf-8")

    @app.get("/static/face/{name:path}")
    def face_asset(name: str) -> Response:
        path = face.resolve_asset(cfg.hub.face_dir, name)
        if path is None:
            raise StarletteHTTPException(status_code=404)
        try:
            body = path.read_bytes()
        except OSError:
            raise StarletteHTTPException(status_code=404) from None
        return Response(body, media_type=face.media_type(path))

    @app.get("/static/hub.css")
    def css() -> Response:
        return Response(CSS, media_type="text/css; charset=utf-8")

    @app.get("/static/hub.js")
    def js() -> Response:
        return Response(JS, media_type="text/javascript; charset=utf-8")

    @app.get("/static/prefs.js")
    def prefs_js() -> Response:
        return Response(PREFS_JS, media_type="text/javascript; charset=utf-8")

    return app


def serve(cfg: Config, port: int, *, clock: Callable[[], datetime] | None = None) -> int:
    """Run on 127.0.0.1 until interrupted. Exit code 0 after a clean stop, 1 if the port is taken."""
    import uvicorn

    print(f"JARVIS hub on http://{LOOPBACK_HOST}:{port}/ (loopback only; the Inbox is its only write path). Stop with Ctrl+C.")
    print("From a phone: put `tailscale serve` in front of this port (docs/hub.md).")
    try:
        uvicorn.run(create_app(cfg, clock=clock, port=port), host=LOOPBACK_HOST, port=port, log_level="warning",
                    access_log=False)
    except SystemExit as exc:  # uvicorn exits with 1 when it cannot bind
        if exc.code not in (0, None):
            print(f"jarvis: the hub could not listen on {LOOPBACK_HOST}:{port}; is it already running? "
                  "Pick another port with --port.", file=sys.stderr)
            return 1
    except OSError as exc:
        print(f"jarvis: the hub could not listen on {LOOPBACK_HOST}:{port}: {exc}", file=sys.stderr)
        return 1
    return 0
