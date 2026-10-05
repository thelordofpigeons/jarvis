"""The FastAPI application: six GET views, two static files and a JSON twin of Status.

Boundaries, all enforced here and tested:
- Loopback only. `serve` passes host 127.0.0.1 and there is no host setting to change it.
  `tailscale serve` proxies a tailnet address to that loopback port.
- A Host header that is not loopback or listed in [hub].allowed_hosts gets 403. That stops a
  web page on another origin from reading the hub through DNS rebinding.
- GET only. Any other method gets 405 from the router; no handler reads a body.
- No CDN: the interactive API docs are switched off, CSS and JS are served from /static/.
- The hub never calls Claude and never writes; its data layer is jarvisd/hub/data.py.

Layer L3 (hub).
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvisd.config import Config
from jarvisd.hub import views
from jarvisd.hub.assets import CSS, JS
from jarvisd.hub.data import HubData

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
                                "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def host_name(header: str) -> str:
    """The host part of a Host header, lower case, port removed, IPv6 brackets kept."""
    text = header.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[: end + 1] if end != -1 else text
    return text.rsplit(":", 1)[0] if ":" in text else text


def create_app(cfg: Config, *, clock: Callable[[], datetime] | None = None) -> FastAPI:
    data = HubData(cfg, clock)
    allowed = LOOPBACK_NAMES | {h.lower() for h in cfg.hub.allowed_hosts}
    refresh = cfg.hub.refresh_s
    app = FastAPI(title="JARVIS hub", docs_url=None, redoc_url=None, openapi_url=None)

    def render(title: str, active: str, content: str, code: int = 200) -> HTMLResponse:
        return HTMLResponse(views.page(title, active, content, flags=data.flags(), refresh_s=refresh), status_code=code)

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

    @app.get("/", response_class=HTMLResponse)
    def today_view() -> HTMLResponse:
        note = data.latest_digest()
        refs: list[dict[str, Any]] = []
        scope = ""
        if note is not None:
            day = note["meta"].get("date")
            refs = data.held(day) if day else []
            scope = f"Recorded for digests of {day}."
            if not refs:
                refs = data.held()
                scope = "All held references currently on file."
        return render("Today", "Today", views.today(note, refs, scope))

    @app.get("/digest/{job_id}", response_class=HTMLResponse)
    def digest_view(job_id: str) -> HTMLResponse:
        note = data.digest_by_id(job_id)
        if note is None:
            return render("Not found", "", views.not_found(404, "No digest note with that id."), 404)
        return render(f"Digest {job_id}", "Runs", views.digest_page(note))

    @app.get("/runs", response_class=HTMLResponse)
    def runs_view() -> HTMLResponse:
        return render("Runs", "Runs", views.runs(data.runs()))

    @app.get("/held", response_class=HTMLResponse)
    def held_view() -> HTMLResponse:
        return render("Held", "Held", views.held(data.held()))

    @app.get("/repos", response_class=HTMLResponse)
    def repos_view() -> HTMLResponse:
        return render("Repos", "Repos", views.repos(data.repos()))

    @app.get("/audit", response_class=HTMLResponse)
    def audit_view() -> HTMLResponse:
        limit = cfg.hub.audit_rows
        content = views.audit(data.audit(), data.audit_rows(limit), data.budget(),
                              data.cost_on(data.now().date()), limit)
        return render("Audit", "Audit", content)

    @app.get("/status", response_class=HTMLResponse)
    def status_view() -> HTMLResponse:
        status = data.status()
        return render("Status", "Status", views.status(data.status_lines(status), status))

    @app.get("/api/status")
    def status_json() -> Response:
        return Response(json.dumps(data.status(), indent=2, ensure_ascii=False, default=str),
                        media_type="application/json")

    @app.get("/static/hub.css")
    def css() -> Response:
        return Response(CSS, media_type="text/css; charset=utf-8")

    @app.get("/static/hub.js")
    def js() -> Response:
        return Response(JS, media_type="text/javascript; charset=utf-8")

    return app


def serve(cfg: Config, port: int, *, clock: Callable[[], datetime] | None = None) -> int:
    """Run on 127.0.0.1 until interrupted. Exit code 0 after a clean stop, 1 if the port is taken."""
    import uvicorn

    print(f"JARVIS hub on http://{LOOPBACK_HOST}:{port}/ (read-only, loopback only). Stop with Ctrl+C.")
    print("From a phone: put `tailscale serve` in front of this port (docs/hub.md).")
    try:
        uvicorn.run(create_app(cfg, clock=clock), host=LOOPBACK_HOST, port=port, log_level="warning",
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
