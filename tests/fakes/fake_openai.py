"""Stand-in for llama-server or llama-swap, served from a thread on 127.0.0.1 (P3).

The local-tier adapter is tested against this and nothing else: no model is downloaded and no
real server is started anywhere in the suite. It speaks just enough of the OpenAI wire format
for the adapter: GET /health and POST /v1/chat/completions. Stdlib only.

`mode` picks what a chat completion returns:
  valid           a correct router contract (or, without response_format, a plain sentence)
  invalid_json    prose that is not JSON
  wrong_schema    JSON that breaks the contract (an unknown language)
  int_confidence  a valid contract whose confidence is the integer 1
  tool_proposal   a valid contract whose needs_tools asks for tools outside the allowlist
  sensitive       a valid contract with sensitive = true
  low_importance  a valid contract that calls everything low importance with high confidence
  truncated       finish_reason "length"
  slow            waits `delay` seconds before answering
  http500         HTTP 500
  redirect        HTTP 302 to another host
  no_content      a response with no choices
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

GOOD = {
    "category": "note",
    "sensitive": False,
    "importance": "low",
    "confidence": 0.91,
    "needs_tools": [],
    "language": "en",
    "reason": "routine item",
}


class FakeOpenAI:
    def __init__(self) -> None:
        self.mode = "valid"
        self.delay = 0.0
        self.health_status = 200
        self.health_body = '{"status":"ok"}'
        self.require_key: str | None = None
        self.router_reply: dict[str, Any] = dict(GOOD)
        self.summary_reply = "A short local summary."
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle --------------------------------------------------------------------

    def start(self) -> "FakeOpenAI":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep pytest output clean
                return

            def _send(self, status: int, body: bytes, ctype: str = "application/json",
                      extra: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                owner.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "body": None})
                if self.path == "/health":
                    self._send(owner.health_status, owner.health_body.encode("utf-8"))
                else:
                    self._send(404, b"{}")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                owner.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body})
                if self.path != "/v1/chat/completions":
                    self._send(404, b"{}")
                    return
                if owner.require_key is not None and self.headers.get("Authorization") != f"Bearer {owner.require_key}":
                    self._send(401, b'{"error":{"message":"bad key"}}')
                    return
                if owner.mode == "redirect":
                    self._send(302, b"", extra={"Location": "http://example.org/steal"})
                    return
                if owner.mode == "http500":
                    self._send(500, b'{"error":{"message":"boom"}}')
                    return
                if owner.mode == "slow":
                    time.sleep(owner.delay)
                if owner.mode == "no_content":
                    self._send(200, b'{"choices":[]}')
                    return
                structured = isinstance(body, dict) and "response_format" in body
                finish = "stop"
                if not structured:
                    content = owner.summary_reply
                elif owner.mode == "invalid_json":
                    content = "I think this item is probably fine, nothing to report."
                elif owner.mode == "wrong_schema":
                    content = json.dumps({**owner.router_reply, "language": "klingon"})
                elif owner.mode == "int_confidence":
                    content = json.dumps({**owner.router_reply, "confidence": 1})
                elif owner.mode == "tool_proposal":
                    content = json.dumps({**owner.router_reply,
                                          "needs_tools": ["read_vault", "send_message", "browser_submit", "rm -rf /"]})
                elif owner.mode == "sensitive":
                    content = json.dumps({**owner.router_reply, "sensitive": True})
                elif owner.mode == "low_importance":
                    content = json.dumps({**owner.router_reply, "importance": "low", "confidence": 0.99})
                elif owner.mode == "truncated":
                    content = '{"category":"note","sens'
                    finish = "length"
                else:
                    content = json.dumps(owner.router_reply)
                reply = {"choices": [{"index": 0, "finish_reason": finish,
                                      "message": {"role": "assistant", "content": content}}]}
                self._send(200, json.dumps(reply).encode("utf-8"))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ---- inspection -------------------------------------------------------------------

    def posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]
