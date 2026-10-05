"""Typed, fail-closed configuration (design section 4, decisions D2 and D10).

`jarvis.toml` is human-owned and tracked. `jarvis.local.toml` is human-owned, gitignored and
holds everything private (repo list, sensitive terms, extra globs). This module only ever
READS both. There is deliberately no function here that writes a config file, and a test
greps the package to keep it that way.

Phase 0 tables (paths, gates, router, ...) use extra="ignore" so the daemon tolerates the
keys the watchdog and the kill switch own. Tables added for v1 use extra="forbid" so a typo
in a new key fails loudly instead of silently reverting to a default.

The same strictness applies to jarvis.local.toml for every table, Phase 0 ones included: the
tracked file may carry keys other programs own, but the private file has one author and holds
the sensitive terms, so an unknown key or table there is a ConfigError (names only).
"""
from __future__ import annotations

import hashlib
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator, model_validator

from jarvisd import ROOT
from jarvisd.common import canonical_json, sha256_hex

# Gate 1 floor. Config can add to this list and can never remove from it (design section 6).
# Globs are matched against canonical forward-slash paths. "**/sensitive/**" also covers the
# rule "any path component equal to sensitive".
SENSITIVE_FLOOR_GLOBS: tuple[str, ...] = (
    "**/brain/telos/**",
    "**/brain/notes/**",
    "**/sensitive/**",
)

# Flags the summarize profile relies on (design section 7). Preflight checks `claude --help`
# for each. Listed here too so a config that omits the key still fails closed on a bad CLI.
DEFAULT_REQUIRED_FLAGS: tuple[str, ...] = (
    "--output-format",
    "--model",
    "--setting-sources",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--tools",
    "--no-session-persistence",
    "--permission-prompts",
    "--max-budget-usd",
    "--system-prompt",
)

_RUN_AT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(Exception):
    """The configuration is missing, unreadable or invalid. Callers must not run jobs."""


class _Ignore(BaseModel):
    """Phase 0 tables: other programs own extra keys here."""

    model_config = ConfigDict(extra="ignore")


class _Forbid(BaseModel):
    """v1 tables: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


# --- Phase 0 tables (extra="ignore") ---------------------------------------------------


class MetaCfg(_Ignore):
    version: str = ""
    phase: str = ""
    machine: str = ""
    owner_account: str = ""
    daemon_account: str = ""


class PathsCfg(_Ignore):
    root: Path
    queue: Path
    logs: Path
    models: Path
    vault_write_raw: Path
    vault_write_sessions: Path
    vault_forbidden: list[Path] = Field(default_factory=list)

    @property
    def brain_root(self) -> Path:
        """The vault root, derived from the one location the daemon may write sessions to."""
        return self.vault_write_sessions.parent


class GatesCfg(_Ignore):
    sensitive_path_globs: list[str] = Field(default_factory=list)
    sensitive_tags: list[str] = Field(default_factory=list)
    importance_escalate: list[str] = Field(default_factory=list)
    # Must be a real float: bin/watchdog.py --self-test checks isinstance(thr, float) too.
    confidence_threshold: float
    # Only ever set in jarvis.local.toml. Matching is case-insensitive and accent-folded in tier.py.
    sensitive_terms: list[str] = Field(default_factory=list)

    @field_validator("confidence_threshold", mode="before")
    @classmethod
    def _must_be_float(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, float):
            raise ValueError("confidence_threshold must be a float such as 0.72")
        if not 0.0 < value < 1.0:
            raise ValueError("confidence_threshold must be strictly between 0 and 1")
        return value


class RouterStubRules(_Forbid):
    financial: list[str] = Field(default_factory=list)
    client_facing: list[str] = Field(default_factory=list)
    irreversible: list[str] = Field(default_factory=list)
    work_prod: list[str] = Field(default_factory=list)

    @field_validator("financial", "client_facing", "irreversible", "work_prod")
    @classmethod
    def _compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return patterns


class RouterStubCfg(_Forbid):
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rules: RouterStubRules = Field(default_factory=RouterStubRules)


class RouterCfg(_Ignore):
    model: str = ""
    fallback_model: str = ""
    contract_fields: list[str] = Field(default_factory=list)
    languages_expected: list[str] = Field(default_factory=list)
    darija_verified: bool = False
    # v1 key. Defaults to "stub" so the tracked jarvis.toml does not have to carry it
    # (the file is append-only for agents, and [router] cannot be reopened at the end).
    adapter: str = Field(default="stub", min_length=1)
    stub: RouterStubCfg = Field(default_factory=RouterStubCfg)


# --- v1 tables (extra="forbid") --------------------------------------------------------


class DaemonCfg(_Forbid):
    tick_seconds: int = Field(default=120, gt=0)
    heartbeat_seconds: int = Field(default=30, gt=0)
    state_dir: Path = ROOT / "state"


class RepoCfg(_Forbid):
    name: str = Field(min_length=1)
    path: Path
    work: bool = False
    counts_only: bool = False


class GithubCfg(_Forbid):
    """[digest.github]: the read-only `gh` collector. Off unless the tracked file turns it on."""

    enabled: StrictBool = False
    max_prs: int = Field(default=10, ge=1, le=50)
    timeout_s: float = Field(default=20.0, gt=0, le=120)


class DigestCfg(_Forbid):
    run_at: str = "06:30"
    window_hours_default: int = Field(default=36, gt=0)
    window_hours_max: int = Field(default=72, gt=0)
    max_payload_bytes: int = Field(default=40000, gt=0)
    max_item_chars: int = Field(default=600, gt=0)
    write_session_note: StrictBool = False
    clickup_enabled: StrictBool = False
    # The ClickUp section (plan T13, docs/v1-operations.md). Off until `jarvis clickup check
    # --live` has passed on this machine. The user id is private: set it in jarvis.local.toml
    # (empty means "the account the claude.ai connector is signed in with").
    clickup_user_id: str = ""
    clickup_max_budget_usd: float = Field(default=0.10, gt=0, le=1.0)
    clickup_timeout_s: int = Field(default=75, gt=0, le=600)
    # Decided by the live check (design D7): allow the ToolSearch tool only if the connector
    # tools are unreachable without it. Costs tokens and widens what the call may do.
    clickup_allow_tool_search: StrictBool = False
    dirty_ignore: list[str] = Field(default_factory=list)
    # Windows scheduled tasks whose last run and result the digest reports under "what JARVIS
    # did while you slept". Names are put into a PowerShell command, so they are restricted.
    watched_tasks: list[str] = Field(default_factory=lambda: ["JarvisDaemon"])
    # Fail closed: work repo metadata reaches Claude only when this is true in the local file (D6).
    work_metadata_to_claude: StrictBool = False
    repos: list[RepoCfg] = Field(default_factory=list)
    github: GithubCfg = Field(default_factory=GithubCfg)

    @field_validator("run_at")
    @classmethod
    def _check_run_at(cls, value: str) -> str:
        if not _RUN_AT_RE.match(value):
            raise ValueError("run_at must be HH:MM in 24 hour local time")
        return value

    @field_validator("watched_tasks")
    @classmethod
    def _check_watched_tasks(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
                raise ValueError("watched_tasks entries are task names: letters, digits, dot, underscore, hyphen")
        return value

    @field_validator("clickup_user_id")
    @classmethod
    def _check_clickup_user_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"[0-9]{1,20}", value):
            raise ValueError("clickup_user_id must be digits only, or empty")
        return value

    @model_validator(mode="after")
    def _check_windows(self) -> "DigestCfg":
        if self.window_hours_default > self.window_hours_max:
            raise ValueError("window_hours_default cannot exceed window_hours_max")
        return self

    def run_at_hm(self) -> tuple[int, int]:
        hour, minute = self.run_at.split(":")
        return int(hour), int(minute)


class ConsolidateCfg(_Forbid):
    """[consolidate]: the nightly memory-candidates pass (spec 6a, jarvisd/consolidate.py).

    Off by default: the pass spends one Claude call a night, so the owner turns it on. The
    first three keys are in jarvis.toml; the rest have code defaults.
    """

    enabled: StrictBool = False
    run_at: str = "02:00"
    max_candidates: int = Field(default=8, ge=1, le=8)
    window_hours_default: int = Field(default=36, gt=0)
    window_hours_max: int = Field(default=72, gt=0)
    max_notes: int = Field(default=30, ge=1, le=200)
    max_lines_per_note: int = Field(default=80, ge=1, le=500)

    @field_validator("run_at")
    @classmethod
    def _check_run_at(cls, value: str) -> str:
        if not _RUN_AT_RE.match(value):
            raise ValueError("run_at must be HH:MM in 24 hour local time")
        return value

    @model_validator(mode="after")
    def _check_windows(self) -> "ConsolidateCfg":
        if self.window_hours_default > self.window_hours_max:
            raise ValueError("window_hours_default cannot exceed window_hours_max")
        return self

    def run_at_hm(self) -> tuple[int, int]:
        hour, minute = self.run_at.split(":")
        return int(hour), int(minute)


class ClaudeCfg(_Forbid):
    binary: str = ""
    model: str = "sonnet"
    timeout_seconds: int = Field(default=180, gt=0)
    max_budget_usd: float = Field(default=0.50, gt=0)
    daily_budget_usd: float = Field(default=2.00, gt=0)
    daily_calls: int = Field(default=6, ge=0)
    isolation_overhead_tokens: int = Field(default=3000, ge=0)
    archive_payloads: StrictBool = False
    required_flags: list[str] = Field(default_factory=lambda: list(DEFAULT_REQUIRED_FLAGS))


class LlamaCfg(_Ignore):
    """Where llama-server (or llama-swap) listens. The Phase 0 [llama] table owns more keys
    (credential name, restart time); the local-tier adapter reads only the address, so the
    watchdog and the daemon cannot drift apart on the port. jarvisd/local.py refuses any host
    other than 127.0.0.1 at use time, not here, so a typo disables the tier loudly instead of
    stopping the whole daemon."""

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


class TrustCfg(_Ignore):
    """Design section 4d. Missing lists are empty: fail closed, nothing is allowed by default."""

    local_model_allowlist: list[str] = Field(default_factory=list)
    claude_allowlist: list[str] = Field(default_factory=list)
    always_confirm: list[str] = Field(default_factory=list)


class LocalCfg(_Forbid):
    enabled: StrictBool = False
    backend: str = ""  # "llama" is the one adapter jarvisd/local.py provides
    # NAME of an environment variable holding the llama-server --api-key. The value never
    # enters a config file, a log or an audit record.
    api_key_env: str = ""
    # Model names as llama-swap (or llama-server) knows them. Empty router_model falls back to
    # [router].model; empty summary_model falls back to router_model.
    router_model: str = ""
    summary_model: str = ""
    # Sensitive items may be summarized by the local model only when this is true. The text
    # never leaves loopback either way; this decides whether the model may read it at all.
    summarize_sensitive: StrictBool = False
    health_timeout_s: float = Field(default=2.0, gt=0)
    request_timeout_s: float = Field(default=30.0, gt=0)
    summary_timeout_s: float = Field(default=120.0, gt=0)
    # Upper bound on one blocking wait for the server (spec 4b). The per-class
    # [queue.classes.*].local_wait_s is the policy; this keeps one wait from freezing the tick.
    max_inline_wait_s: int = Field(default=30, ge=0)
    # After a wait gave up, or while degraded, do not wait or ask again for this long.
    retry_cooldown_s: int = Field(default=60, ge=0)
    # Consecutive bad replies (transport error, timeout, invalid contract) that mark the tier degraded.
    degrade_after: int = Field(default=2, ge=1)
    max_input_chars: int = Field(default=4000, gt=0)

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value):
            raise ValueError("api_key_env must be an environment variable NAME such as JARVIS_LLAMA_KEY")
        return value


_NTFY_URL_RE = re.compile(r"^(https?)://([^\s/?#@:]+|\[[0-9a-fA-F:]+\])(:\d+)?(/[^\s?#]*)?$")
_LOOPBACK_HOST_RE = re.compile(r"^(localhost|127(\.\d{1,3}){3}|\[::1\])$")


def check_ntfy_url(value: str) -> str:
    """Validate an ntfy base address; plain http is allowed only for a loopback host.

    The bearer token and the digest line travel in this request, so they must not cross a
    network unencrypted. A server on another machine is reached over https (for example
    through `tailscale serve`); a server on this machine may use http on loopback.
    """
    found = _NTFY_URL_RE.match(value)
    if found is None:
        raise ValueError("ntfy_url must be an http or https address without credentials, query or fragment")
    if found.group(1) == "http" and not _LOOPBACK_HOST_RE.match(found.group(2).casefold()):
        raise ValueError("ntfy_url must use https unless the host is loopback (localhost, 127.x, [::1])")
    return value


class NotifyCfg(_Forbid):
    adapter: str = "toast"  # toast | null | ntfy | multi (ntfy plus toast)
    toast_script: Path = ROOT / "deploy" / "notify-jarvis.ps1"
    # ntfy (docs/notify-ntfy.md). The server address and the topic are private to the owner
    # and live in jarvis.local.toml; the token VALUE never does, only the name of the
    # environment variable that holds it.
    ntfy_url: str = ""
    ntfy_topic: str = ""
    ntfy_token_env: str = ""
    ntfy_priority: int = Field(default=3, ge=1, le=5)
    ntfy_timeout_s: float = Field(default=10.0, gt=0, le=60)
    # Click target of the push. {vault} is the vault folder name, {path} the note path inside
    # it without the .md suffix. Empty disables the click action.
    ntfy_click_template: str = "obsidian://open?vault={vault}&file={path}"

    @field_validator("ntfy_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return check_ntfy_url(value) if value else value

    @field_validator("ntfy_topic")
    @classmethod
    def _check_topic(cls, value: str) -> str:
        if value and not re.match(r"^[A-Za-z0-9_-]{1,64}$", value):
            raise ValueError("ntfy_topic may only contain letters, digits, underscore and hyphen")
        return value

    @field_validator("ntfy_token_env")
    @classmethod
    def _check_token_env(cls, value: str) -> str:
        if value and not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", value):
            raise ValueError("ntfy_token_env must be an environment variable name, not the token")
        return value


class RetentionCfg(_Forbid):
    audit_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    audit_keep_days: int = Field(default=180, gt=0)
    done_job_days: int = Field(default=60, gt=0)
    failed_job_days: int = Field(default=60, gt=0)
    runs_days: int = Field(default=30, gt=0)
    held_days: int = Field(default=14, gt=0)
    logs_warn_mb: int = Field(default=500, gt=0)
    disk_warn_gb: int = Field(default=10, gt=0)


class QueueClassCfg(_Forbid):
    timeout_s: int = Field(default=1800, gt=0)
    local_wait_s: int = Field(default=600, ge=0)


class QueueCfg(_Forbid):
    classes: dict[str, QueueClassCfg] = Field(
        default_factory=lambda: {"background_batch": QueueClassCfg()}
    )


class HubCfg(_Forbid):
    """[hub]: the read-only cockpit (jarvisd/hub, docs/hub.md). There is no host key on purpose:
    the hub binds 127.0.0.1 and nothing else, and `tailscale serve` is how a phone reaches it."""

    port: int = Field(default=8765, ge=1024, le=65535)
    # Host header names accepted besides localhost, 127.0.0.1 and [::1]. `tailscale serve`
    # forwards the tailnet name, which is private, so the owner lists it in jarvis.local.toml.
    allowed_hosts: list[str] = Field(default_factory=list)
    # Seconds between automatic refreshes of an open page; 0 turns the refresh script off.
    refresh_s: int = Field(default=30, ge=0, le=3600)
    audit_rows: int = Field(default=50, ge=1, le=500)

    @field_validator("allowed_hosts")
    @classmethod
    def _host_names(cls, value: list[str]) -> list[str]:
        for host in value:
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
                raise ValueError("allowed_hosts entries are bare host names, no scheme, port or path")
        return value


class HygieneCfg(_Forbid):
    # Private denylist for the hygiene test. Lives only in jarvis.local.toml (D10).
    deny_substrings: list[str] = Field(default_factory=list)


class Config(BaseModel):
    """The validated configuration. Mutating it in memory is allowed (tests do); writing it is not."""

    model_config = ConfigDict(extra="ignore")

    meta: MetaCfg = Field(default_factory=MetaCfg)
    paths: PathsCfg
    gates: GatesCfg
    router: RouterCfg = Field(default_factory=RouterCfg)
    daemon: DaemonCfg = Field(default_factory=DaemonCfg)
    digest: DigestCfg = Field(default_factory=DigestCfg)
    claude: ClaudeCfg = Field(default_factory=ClaudeCfg)
    local: LocalCfg = Field(default_factory=LocalCfg)
    llama: LlamaCfg = Field(default_factory=LlamaCfg)
    trust: TrustCfg = Field(default_factory=TrustCfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    queue: QueueCfg = Field(default_factory=QueueCfg)
    hygiene: HygieneCfg = Field(default_factory=HygieneCfg)
    consolidate: ConsolidateCfg = Field(default_factory=ConsolidateCfg)
    hub: HubCfg = Field(default_factory=HubCfg)
    # sha256 of the merged raw config. Recorded in every job so a run names its own rules.
    sha256: str = ""

    def sensitive_globs(self) -> list[str]:
        """Hardcoded floor plus configured globs, floor first, no duplicates.

        Config can only add. Emptying `[gates].sensitive_path_globs` leaves the floor.
        """
        merged: list[str] = list(SENSITIVE_FLOOR_GLOBS)
        for glob in self.gates.sensitive_path_globs:
            if glob not in merged:
                merged.append(glob)
        return merged


# --- loading ---------------------------------------------------------------------------


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def expand_path(value: str | Path, base: Path) -> Path:
    """Turn a configured path into an absolute one, the same way on every machine.

    `~` (alone, or followed by a slash) is the current user's home, so the tracked file can
    name the vault as `~/brain` without naming a person. A relative value is relative to
    `base`, the folder holding jarvis.toml (the repo root), so `queue` means this checkout's
    queue. Absolute values pass through. Symlinks are not resolved: the path guards in
    tier.py and vault.py canonicalise on their own and this must not hide a junction from them.
    """
    text = str(value)
    if text == "~" or text.startswith(("~/", "~\\")):
        text = os.path.expanduser("~") + text[1:]
    path = Path(text)
    if not path.is_absolute():
        path = Path(base) / path
    return Path(os.path.normpath(path))


# Path-valued keys of the tables the daemon reads. Phase 0 tables the daemon ignores
# (killswitch, audit, sandbox) are expanded by bin/watchdog.py for its own keys.
_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "paths": ("root", "queue", "logs", "models", "vault_write_raw", "vault_write_sessions"),
    "daemon": ("state_dir",),
    "notify": ("toast_script",),
}


def _expand_raw_paths(raw: dict[str, Any], base: Path) -> dict[str, Any]:
    """Expand `~` and repo-relative values in a merged raw config. Returns a new dict.

    Values are stored back as forward-slash strings so the config stays JSON-hashable.
    """
    out = dict(raw)
    for table, keys in _PATH_KEYS.items():
        section = out.get(table)
        if not isinstance(section, dict):
            continue
        section = dict(section)
        for key in keys:
            if isinstance(section.get(key), str):
                section[key] = expand_path(section[key], base).as_posix()
        if table == "paths" and isinstance(section.get("vault_forbidden"), list):
            section["vault_forbidden"] = [
                expand_path(v, base).as_posix() if isinstance(v, str) else v for v in section["vault_forbidden"]
            ]
        out[table] = section
    digest = out.get("digest")
    if isinstance(digest, dict) and isinstance(digest.get("repos"), list):
        repos = []
        for repo in digest["repos"]:
            if isinstance(repo, dict) and isinstance(repo.get("path"), str):
                repo = {**repo, "path": expand_path(repo["path"], base).as_posix()}
            repos.append(repo)
        out["digest"] = {**digest, "repos": repos}
    return out


# Config keys renamed in 1.1.0, held as sha256 of the old key so that no tracked file spells
# the old name (it was the name of the owner's employer). A jarvis.local.toml written before
# the rename keeps loading: the old key is rewritten to the new one wherever it appears.
_RENAMED_KEYS: dict[str, str] = {
    "97e3efbcb9c13ef2356e6f7699594985da3081235558670b9124e038119dd983": "work",
    "771d9ec2165f30c2827b3eea19b3226376c071115bac5908b4004be32bcd65be": "work_metadata_to_claude",
    "6c2a841aa1da2232f857f2540368fc90881da4164afd0210a0990cab14b7f9ab": "work_prod",
}


def upgrade_renamed_keys(data: Any) -> Any:
    """A copy of a parsed config with keys renamed in 1.1.0 rewritten to their new names.

    Walks tables and lists of tables. Having an old key and its new spelling in the same table
    is an error, never a silent pick: the two could disagree about a privacy switch.
    """
    if isinstance(data, list):
        return [upgrade_renamed_keys(v) for v in data]
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        new = _RENAMED_KEYS.get(hashlib.sha256(str(key).encode("utf-8")).hexdigest(), key)
        if new in out or (new != key and new in data):
            raise ConfigError(f"both an old and the new spelling of {new!r} are set: keep only {new!r}")
        out[new] = upgrade_renamed_keys(value)
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not valid UTF-8: {exc}") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _merge(base: dict[str, Any], over: dict[str, Any], path: tuple[str, ...] = ()) -> dict[str, Any]:
    """Deep merge `over` into a copy of `base`.

    Tables merge key by key. Lists under [gates] append (so the local file can add sensitive
    globs, tags and terms but never remove a tracked one), and so does [paths].vault_forbidden
    (the tracked file names only the generic vault folders; a private work folder is added
    locally and the generic ones can never be dropped). Everything else, scalars and other
    lists, is overridden by the local value.
    """
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _merge(current, value, path + (key,))
        elif (
            (path == ("gates",) or (path == ("paths",) and key == "vault_forbidden"))
            and isinstance(current, list)
            and isinstance(value, list)
        ):
            out[key] = current + [item for item in value if item not in current]
        else:
            out[key] = value
    return out


def _unknown_local_keys(model: type[BaseModel], data: dict[str, Any], prefix: str = "") -> list[str]:
    """Dotted names of keys in the local file that the schema does not know.

    Only the extra="ignore" tables need this: pydantic already rejects unknown keys in the
    v1 tables. The tracked file may carry keys other programs own, but jarvis.local.toml has
    one author and the privacy-critical lists (sensitive_terms, extra globs) live only
    there, so a misspelled key must not silently turn into "no terms configured" (D10).
    """
    if model.model_config.get("extra") != "ignore":
        return []
    fields = model.model_fields
    found: list[str] = []
    for key, value in data.items():
        name = f"{prefix}{key}"
        field = fields.get(key)
        if field is None or (not prefix and key == "sha256"):
            found.append(name)
            continue
        inner = field.annotation
        if isinstance(inner, type) and issubclass(inner, BaseModel) and isinstance(value, dict):
            found.extend(_unknown_local_keys(inner, value, f"{name}."))
    return found


def build_config(raw: dict[str, Any], base: Path | None = None) -> Config:
    """Validate an already merged dict into a Config. Used by load_config and by test fixtures.

    Path values are expanded here (see expand_path), against `base` or the repo root, so a
    caller that builds a Config from the tracked file's raw dict never sees a cwd-relative path.
    Expansion is idempotent: absolute values pass through unchanged.
    """
    if not isinstance(raw.get("gates"), dict):
        raise ConfigError("no [gates] table: refusing to run without gate settings (fail closed)")
    raw = _expand_raw_paths(raw, ROOT if base is None else base)
    try:
        cfg = Config.model_validate(raw)
        cfg.sha256 = sha256_hex(canonical_json(raw))
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {_format_errors(exc)}") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"configuration cannot be hashed: {exc}") from exc
    return cfg


def load_config(
    path: Path | str | None = None,
    *,
    local_path: Path | str | None = None,
    use_local: bool = True,
) -> Config:
    """Load jarvis.toml, merge jarvis.local.toml beside it when present, and validate.

    `use_local=False` ignores the local file, which tests use so a developer's private
    overrides cannot change an assertion about the tracked defaults.
    """
    base_path = Path(path) if path is not None else ROOT / "jarvis.toml"
    raw = _read_toml(base_path)
    # The tracked file must carry [gates] itself: a local file alone cannot supply it.
    if not isinstance(raw.get("gates"), dict):
        raise ConfigError(f"{base_path} has no [gates] table: refusing to run (fail closed)")
    if use_local:
        local = Path(local_path) if local_path is not None else base_path.with_name("jarvis.local.toml")
        if local.is_file():
            local_raw = upgrade_renamed_keys(_read_toml(local))
            unknown = _unknown_local_keys(Config, local_raw)
            if unknown:
                # Names only, never values: a misplaced sensitive term must not reach a log.
                raise ConfigError(f"{local} has unknown key(s): {', '.join(unknown)}")
            raw = _merge(raw, local_raw)
    return build_config(raw, Path(os.path.abspath(base_path)).parent)
