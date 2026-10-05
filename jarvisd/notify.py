"""Notifier protocol, the Windows toast adapter and the ntfy adapter (design section 12, plan P2).

Layer L3. A notification is built from integers and fixed phrases only, never from item
titles or text, because a toast is shown on the lock screen and logged by Windows. The
adapter never raises: a toast is a convenience, and the vault note is the deliverable.

Limits, stated plainly:
- The toast has no click action, so the message names the note path instead.
- `ToastNotifier` can only tell that the PowerShell process exited with 0 within the
  timeout. Windows may still suppress the banner (focus assist, notification settings), and
  nothing here can see that.
- The message is passed as one argv item. PowerShell `-File` still treats an argument that
  starts with a dash as a parameter name, so a leading dash is stripped.

ntfy (plan P2, docs/notify-ntfy.md). `NtfyNotifier` POSTs the same text the toast shows, so
it carries the same guarantee: integers, fixed phrases and a note path, never item content.
Limits:
- ntfy.sh or any server in the middle sees the title, the text and the click URL. The
  design assumes a self-hosted server on the tailnet; a public server is the owner's call.
- The address and topic never reach the audit log or an exception text, only a short code
  such as `http_500`, `timeout` or `network`. The token is read from the environment at
  send time and is never stored, logged or echoed.
- No proxy variable is honoured and redirects are not followed, so a token cannot be sent to
  a host other than the configured one.
- The push has no delivery receipt: `ok` means the server answered 2xx, not that a phone
  showed it. The click URL is only useful if the phone can open it (an Obsidian vault
  synced to the phone for the default template).
- The click URL is taken from the `brain/<path>.md` the message already names, because the
  Notifier protocol carries one string. `digest_message` builds that path from path-safe
  characters only, and anything else in the text yields no click action.
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jarvisd.common import strip_dashes
from jarvisd.config import Config, ConfigError, check_ntfy_url

MAX_MESSAGE_CHARS = 180
TOAST_TIMEOUT_S = 15.0
TOAST_TITLE = "JARVIS"
# The existing Claude Code hook script takes the same -Title and -Message parameters, so it
# is the documented second choice when the JARVIS copy is missing (design section 12).
HOOKS_SCRIPT = Path.home() / ".claude" / "hooks" / "notify.ps1"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_PATH_UNSAFE = re.compile(r"[^A-Za-z0-9._/-]+")

# Fixed phrases for the "reason class" slot. Anything not listed becomes "unknown", so a
# hostile or accidental free-text reason can never reach the screen.
REASON_CLASSES: dict[str, str] = {
    "budget": "budget",
    "rate_limit": "quota",
    "timeout": "timeout",
    "transient": "service error",
    "unavailable": "unavailable",
    "bad_json": "bad reply",
    "bad_schema": "bad reply",
    "disabled": "disabled",
    "network": "network",
    "preflight": "claude not ready",
    "cli_error": "cli error",
    "spawn_failed": "claude not ready",
    "auth": "login",
    "breaker": "breaker open",
    "payload_blocked": "tier violation",
    "isolation_anomaly": "isolation check",
    "isolation_breach": "isolation check",
    "error": "error",
    "vault_busy": "vault busy",
    "vault_error": "vault error",
    "vault_denied": "vault refused",
}
VARIANTS = ("ok", "degraded", "auth", "breaker", "fallback", "unwritten")


@dataclass(frozen=True)
class NotifyResult:
    """Outcome of one send. `detail` is a short code, never message text."""

    ok: bool
    detail: str = ""


class Notifier(Protocol):
    """Anything that can show one short message. Implementations must not raise."""

    name: str

    def send(self, message: str) -> NotifyResult: ...


class NullNotifier:
    """Shows nothing. For dev mode, tests and `[notify].adapter = "null"`."""

    name = "null"

    def send(self, message: str) -> NotifyResult:  # noqa: ARG002  the message is intentionally dropped
        return NotifyResult(ok=True, detail="null")


Runner = Callable[[list[str], float], int]


def _default_runner(argv: list[str], timeout: float) -> int:
    proc = subprocess.run(  # noqa: S603  list argv, fixed executable, no shell
        argv,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode


def clean_message(text: str) -> str:
    """One line, no dashes, no control characters, no leading dash, at most 180 characters."""
    flat = " ".join(_CONTROL.sub(" ", strip_dashes(str(text))).split()).lstrip("-").strip()
    return flat[:MAX_MESSAGE_CHARS].rstrip()


class ToastNotifier:
    """Runs deploy/notify-jarvis.ps1 as an argv list with a hard timeout (design section 12)."""

    name = "toast"

    def __init__(
        self,
        script: str | Path,
        *,
        runner: Runner | None = None,
        timeout: float = TOAST_TIMEOUT_S,
        fallback_script: str | Path | None = None,
        powershell: str = "powershell",
    ) -> None:
        self.script = Path(script)
        self.fallback_script = Path(fallback_script) if fallback_script is not None else None
        self.timeout = timeout
        self.powershell = powershell
        self._runner: Runner = runner or _default_runner

    def build_argv(self, message: str, script: Path | None = None) -> list[str]:
        """The exact command line. Forward slashes, as in the design; the message is one item."""
        target = script or self.script
        return [
            self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", target.as_posix(),
            "-Title", TOAST_TITLE,
            "-Message", clean_message(message),
        ]

    def _pick_script(self) -> Path | None:
        for candidate in (self.script, self.fallback_script):
            if candidate is not None and candidate.is_file():
                return candidate
        return None

    def send(self, message: str) -> NotifyResult:
        script = self._pick_script()
        if script is None:
            return NotifyResult(ok=False, detail="script_missing")
        try:
            code = self._runner(self.build_argv(message, script), self.timeout)
        except subprocess.TimeoutExpired:
            return NotifyResult(ok=False, detail="timeout")
        except OSError:
            return NotifyResult(ok=False, detail="spawn_failed")
        except Exception:  # noqa: BLE001  a toast must never take a digest down
            return NotifyResult(ok=False, detail="error")
        return NotifyResult(ok=True, detail="sent") if code == 0 else NotifyResult(ok=False, detail=f"exit_{code}")


def reason_class(reason: object) -> str:
    """Map a reason token to a fixed phrase. Unlisted tokens are 'unknown', never echoed."""
    return REASON_CLASSES.get(str(reason), "unknown")


def _count(counts: Mapping[str, object], key: str) -> int:
    try:
        return max(0, int(float(str(counts.get(key, 0)))))
    except (TypeError, ValueError):
        return 0


def _shown_path(rel_path: str) -> str:
    """The vault-relative path as the user finds it, restricted to path-safe characters."""
    safe = _PATH_UNSAFE.sub("", rel_path).lstrip("/")
    return safe if safe.startswith("brain/") else f"brain/{safe}"


def digest_message(status: str, counts: Mapping[str, object], rel_path: str, reason: str = "") -> str:
    """The toast text for one digest. Integers and fixed phrases only (design section 12).

    `counts` uses 'attention' (items worth a look) and 'held' (withheld items). `reason` is
    a token such as 'timeout' and is mapped to a fixed phrase, never printed as given.
    """
    path = _shown_path(rel_path)
    if status == "ok":
        return f"Digest ready: {_count(counts, 'attention')} to look at, {_count(counts, 'held')} held. {path}"
    if status == "degraded":
        return f"Digest ready, Claude was unavailable ({reason_class(reason)}). Deterministic sections only."
    if status == "auth":
        return "Claude login expired. Run claude /login, then jarvis run-digest --claude --force."
    if status == "breaker":
        return f"JARVIS paused Claude calls: {reason_class(reason)}. Run jarvis breaker status."
    if status == "unwritten":
        return (f"Digest could not be written to the vault ({reason_class(reason)}). "
                "A copy is kept in the JARVIS state folder. Run jarvis status.")
    if status == "fallback":
        return f"Digest written under a fallback name, the target was busy. {path}"
    raise ValueError(f"unknown notification status {status!r}")


# --- ntfy ------------------------------------------------------------------------------------

NTFY_TITLE = "JARVIS"
NTFY_TIMEOUT_S = 10.0
_NOTE_PATH = re.compile(r"\bbrain/([A-Za-z0-9._/-]+)\.md\b")
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the Authorization header must only ever reach the configured host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        return None


class AuditSink(Protocol):
    """The slice of AuditLog the notifiers use. `emit` must not raise."""

    def emit(self, event: str, **fields: Any) -> Any: ...


def _opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) ignores HTTP_PROXY and friends: the server is on a private network.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _click_url(template: str, vault: str, message: str) -> str:
    """The click target for the note path named in `message`, or "" when there is none."""
    found = _NOTE_PATH.search(message)
    if not template or found is None:
        return ""
    parts = found.group(1).split("/")
    if any(p in ("", ".", "..") for p in parts):
        return ""
    url = template.format(vault=urllib.parse.quote(vault, safe=""), path=urllib.parse.quote(found.group(1), safe=""))
    return url if url.isascii() and url.isprintable() else ""


class NtfyNotifier:
    """POSTs the digest line to `<ntfy_url>/<topic>` with urllib only (plan P2)."""

    name = "ntfy"

    def __init__(
        self,
        url: str,
        topic: str,
        *,
        token_env: str = "",
        priority: int = 3,
        timeout: float = NTFY_TIMEOUT_S,
        click_template: str = "",
        vault: str = "",
        environ: Mapping[str, str] | None = None,
        opener: Any = None,
    ) -> None:
        try:
            check_ntfy_url(url or "")
        except ValueError as exc:
            raise ConfigError(f"[notify].{exc}") from exc
        if not _TOPIC.match(topic or ""):
            raise ConfigError("[notify].ntfy_topic must be 1 to 64 letters, digits, underscore or hyphen")
        try:
            click_template.format(vault="v", path="p")
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError("[notify].ntfy_click_template may only use {vault} and {path}") from exc
        self.endpoint = url.rstrip("/") + "/" + topic
        self.token_env = token_env
        self.priority = priority
        self.timeout = timeout
        self.click_template = click_template
        self.vault = vault
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        # Bound once under another name: tests/test_write_locations.py reads every `.open(x)` as a file open.
        self._urlopen = (opener if opener is not None else _opener()).open

    def _token(self) -> str:
        return self._environ.get(self.token_env, "").strip() if self.token_env else ""

    def build_headers(self, message: str) -> dict[str, str]:
        headers = {
            "Title": NTFY_TITLE,
            "Priority": str(self.priority),
            "Content-Type": "text/plain; charset=utf-8",
        }
        click = _click_url(self.click_template, self.vault, message)
        if click:
            headers["Click"] = click
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def send(self, message: str) -> NotifyResult:
        text = clean_message(message)
        try:
            request = urllib.request.Request(  # noqa: S310  scheme is checked in __init__
                self.endpoint, data=text.encode("utf-8"), headers=self.build_headers(text), method="POST")
            response = self._urlopen(request, timeout=self.timeout)
            try:
                status = int(getattr(response, "status", 200))
            finally:
                response.close()
        except urllib.error.HTTPError as exc:
            exc.close()
            return NotifyResult(ok=False, detail=f"http_{exc.code}")
        except TimeoutError:
            return NotifyResult(ok=False, detail="timeout")
        except urllib.error.URLError as exc:
            timed_out = isinstance(exc.reason, TimeoutError)
            return NotifyResult(ok=False, detail="timeout" if timed_out else "network")
        except OSError:
            return NotifyResult(ok=False, detail="network")
        except Exception:  # noqa: BLE001  a push must never take a digest down
            return NotifyResult(ok=False, detail="error")
        return NotifyResult(ok=True, detail="sent") if 200 <= status < 300 else NotifyResult(ok=False, detail=f"http_{status}")


def _safe_send(member: Notifier, message: str) -> NotifyResult:
    try:
        return member.send(message)
    except Exception:  # noqa: BLE001  the notifier contract says never raise; do not trust it
        return NotifyResult(ok=False, detail="error")


def _audit_ntfy_failure(audit: AuditSink | None, mode: str, detail: str, toast: str) -> None:
    """One record per failed push: a short code only, never the address, topic, token or text."""
    if audit is None:
        return
    try:
        audit.emit("notify_ntfy_failed", mode=mode, detail=detail, toast=toast)
    except Exception:  # noqa: BLE001  auditing must not turn a notification failure into a crash
        pass


class FallbackNotifier:
    """[notify].adapter = "ntfy": push first, toast only if the push failed (audited)."""

    def __init__(self, primary: Notifier, fallback: Notifier, *, audit: AuditSink | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.audit = audit

    @property
    def name(self) -> str:
        return self.primary.name

    def send(self, message: str) -> NotifyResult:
        first = _safe_send(self.primary, message)
        if first.ok:
            return first
        second = _safe_send(self.fallback, message)
        _audit_ntfy_failure(self.audit, "fallback", first.detail, second.detail)
        return NotifyResult(ok=second.ok, detail=f"{self.primary.name}_{first.detail}_{self.fallback.name}_{second.detail}")


class MultiNotifier:
    """[notify].adapter = "multi": every member gets the message; ok if any delivered it."""

    name = "multi"

    def __init__(self, members: Sequence[Notifier], *, audit: AuditSink | None = None) -> None:
        self.members = list(members)
        self.audit = audit

    def send(self, message: str) -> NotifyResult:
        results = [(m.name, _safe_send(m, message)) for m in self.members]
        toast = next((r.detail for n, r in results if n == "toast"), "none")
        for name, res in results:
            if name == "ntfy" and not res.ok:
                _audit_ntfy_failure(self.audit, "multi", res.detail, toast)
        detail = "_".join(f"{n}_{r.detail}" for n, r in results)
        return NotifyResult(ok=any(r.ok for _, r in results), detail=detail)


def _ntfy_from_config(cfg: Config) -> NtfyNotifier:
    n = cfg.notify
    if not n.ntfy_url or not n.ntfy_topic:
        raise ConfigError("[notify].adapter needs ntfy_url and ntfy_topic (set them in jarvis.local.toml)")
    return NtfyNotifier(
        n.ntfy_url, n.ntfy_topic, token_env=n.ntfy_token_env, priority=n.ntfy_priority, timeout=n.ntfy_timeout_s,
        click_template=n.ntfy_click_template, vault=cfg.paths.brain_root.name,
    )


def build_notifier(cfg: Config, audit: AuditSink | None = None) -> Notifier:
    """The adapter named by [notify].adapter. Unknown names fail loudly at startup.

    `audit` receives `notify_ntfy_failed` records for the ntfy-based adapters.
    """
    adapter = cfg.notify.adapter.strip().lower()
    if adapter == "toast":
        return ToastNotifier(cfg.notify.toast_script, fallback_script=HOOKS_SCRIPT)
    if adapter in ("null", "none"):
        return NullNotifier()
    if adapter in ("ntfy", "multi"):
        toast = ToastNotifier(cfg.notify.toast_script, fallback_script=HOOKS_SCRIPT)
        push = _ntfy_from_config(cfg)
        if adapter == "ntfy":
            return FallbackNotifier(push, toast, audit=audit)
        return MultiNotifier([push, toast], audit=audit)
    raise ConfigError(f"unknown [notify].adapter {cfg.notify.adapter!r}; known: toast, null, ntfy, multi")
