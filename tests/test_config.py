"""Tests for jarvisd.config."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from jarvisd import ROOT
from jarvisd.config import SENSITIVE_FLOOR_GLOBS, Config, ConfigError, build_config, load_config

REAL_TOML = ROOT / "jarvis.toml"

MINIMAL = """
[gates]
sensitive_path_globs = ["**/brain/telos/sensitive/**"]
sensitive_tags = ["sensitive"]
importance_escalate = ["financial"]
confidence_threshold = 0.72

[paths]
root = "C:/x/jarvis"
queue = "C:/x/jarvis/queue"
logs = "C:/x/jarvis/logs"
models = "C:/x/jarvis/models"
vault_write_raw = "C:/x/brain/raw/jarvis"
vault_write_sessions = "C:/x/brain/sessions"
vault_forbidden = ["C:/x/brain/telos"]
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def test_real_toml_loads_with_defaults() -> None:
    cfg = load_config(REAL_TOML, use_local=False)
    assert cfg.digest.work_metadata_to_claude is False
    assert cfg.claude.model == "sonnet"
    assert cfg.router.adapter == "stub"
    assert re.fullmatch(r"[0-9a-f]{64}", cfg.sha256)
    assert isinstance(cfg.gates.confidence_threshold, float)
    assert cfg.gates.confidence_threshold == 0.72


def test_missing_gates_table_raises(tmp_path: Path) -> None:
    text = MINIMAL.split("[gates]")[0] + MINIMAL.split("confidence_threshold = 0.72")[1]
    assert "[gates]" not in text
    with pytest.raises(ConfigError, match="gates"):
        load_config(write(tmp_path, "jarvis.toml", text), use_local=False)


def test_minimal_toml_gets_all_new_table_defaults(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "jarvis.toml", MINIMAL), use_local=False)
    assert cfg.daemon.tick_seconds == 120
    assert cfg.digest.run_at == "06:30"
    assert cfg.digest.window_hours_max == 72
    assert cfg.digest.max_payload_bytes == 40000
    assert cfg.digest.clickup_enabled is False
    assert cfg.claude.daily_calls == 6
    assert cfg.claude.max_budget_usd == 0.50
    assert cfg.local.enabled is False
    assert cfg.notify.adapter == "toast"
    assert cfg.queue.classes["background_batch"].timeout_s == 1800
    assert cfg.router.stub.confidence == 0.0


def test_real_toml_has_appended_tables() -> None:
    cfg = load_config(REAL_TOML, use_local=False)
    assert cfg.daemon.heartbeat_seconds == 30
    assert cfg.claude.required_flags
    assert cfg.queue.classes["background_batch"].local_wait_s == 600
    for bucket in ("financial", "client_facing", "irreversible", "work_prod"):
        patterns = getattr(cfg.router.stub.rules, bucket)
        assert patterns, bucket
        for p in patterns:
            re.compile(p)
    assert cfg.digest.repos == []  # repos live only in jarvis.local.toml (D10)


def test_phase0_tables_ignore_unknown_keys(tmp_path: Path) -> None:
    text = MINIMAL.replace("confidence_threshold = 0.72", "confidence_threshold = 0.72\nfuture_key = 1")
    cfg = load_config(write(tmp_path, "jarvis.toml", text), use_local=False)
    assert cfg.gates.confidence_threshold == 0.72


def test_new_tables_forbid_unknown_keys(tmp_path: Path) -> None:
    text = MINIMAL + "\n[digest]\nrun_at = \"06:30\"\nbogus = 1\n"
    with pytest.raises(ConfigError, match="bogus"):
        load_config(write(tmp_path, "jarvis.toml", text), use_local=False)


@pytest.mark.parametrize("bad", ["1", '"0.72"', "0.0", "1.0", "1.5"])
def test_confidence_threshold_must_be_a_float_in_open_unit_interval(tmp_path: Path, bad: str) -> None:
    text = MINIMAL.replace("confidence_threshold = 0.72", f"confidence_threshold = {bad}")
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "jarvis.toml", text), use_local=False)


def test_work_flag_is_strict_bool(tmp_path: Path) -> None:
    text = MINIMAL + '\n[digest]\nwork_metadata_to_claude = "false"\n'
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "jarvis.toml", text), use_local=False)


def test_invalid_toml_and_missing_file_raise_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "jarvis.toml", "[gates\nx="), use_local=False)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml", use_local=False)


def test_bad_run_at_and_bad_regex_raise(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "a.toml", MINIMAL + '\n[digest]\nrun_at = "25:99"\n'), use_local=False)
    bad_rx = MINIMAL + "\n[router.stub.rules]\nfinancial = [\"(unclosed\"]\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "b.toml", bad_rx), use_local=False)


def test_local_override_merges_gates_lists_and_overrides_scalars(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(
        tmp_path,
        "jarvis.local.toml",
        """
[gates]
sensitive_terms = ["Zorglub"]
sensitive_path_globs = ["**/synthetic-secret/**"]
sensitive_tags = ["nda"]
confidence_threshold = 0.80

[digest]
work_metadata_to_claude = true
run_at = "07:15"
repos = [
  { name = "demo-api", path = "C:/synthetic/demo-api", work = true, counts_only = false },
  { name = "notes-repo", path = "C:/synthetic/notes-repo", counts_only = true },
]

[hygiene]
deny_substrings = ["SYNTHETIC-DENY"]
""",
    )
    plain = load_config(base, use_local=False)
    cfg = load_config(base)
    # Lists under [gates] append (the floor of the tracked file stays).
    assert cfg.gates.sensitive_path_globs == ["**/brain/telos/sensitive/**", "**/synthetic-secret/**"]
    assert cfg.gates.sensitive_tags == ["sensitive", "nda"]
    assert cfg.gates.sensitive_terms == ["Zorglub"]
    # Scalars override.
    assert cfg.gates.confidence_threshold == 0.80
    assert cfg.digest.run_at == "07:15"
    assert cfg.digest.work_metadata_to_claude is True
    assert [r.name for r in cfg.digest.repos] == ["demo-api", "notes-repo"]
    assert cfg.digest.repos[0].work is True
    assert cfg.digest.repos[1].counts_only is True
    assert cfg.hygiene.deny_substrings == ["SYNTHETIC-DENY"]
    # The hash covers the merged result, so an override changes it.
    assert cfg.sha256 != plain.sha256
    assert plain.sha256 == load_config(base, use_local=False).sha256


def test_local_override_with_unknown_new_key_raises(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", "[claude]\nmodle = \"opus\"\n")
    with pytest.raises(ConfigError, match="modle"):
        load_config(base)


def test_local_override_cannot_remove_tracked_gate_entries(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", "[gates]\nsensitive_path_globs = []\nsensitive_tags = []\n")
    cfg = load_config(base)
    assert "**/brain/telos/sensitive/**" in cfg.gates.sensitive_path_globs
    assert cfg.gates.sensitive_tags == ["sensitive"]


def test_sensitive_globs_merge_floor_with_config(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "jarvis.toml", MINIMAL), use_local=False)
    globs = cfg.sensitive_globs()
    for floor in SENSITIVE_FLOOR_GLOBS:
        assert floor in globs
    assert "**/brain/telos/sensitive/**" in globs
    assert len(globs) == len(set(globs))
    # Config can only add: emptying the config list leaves the floor.
    cfg.gates.sensitive_path_globs = []
    assert set(cfg.sensitive_globs()) == set(SENSITIVE_FLOOR_GLOBS)


def test_floor_covers_telos_notes_and_any_sensitive_component() -> None:
    joined = " ".join(SENSITIVE_FLOOR_GLOBS)
    assert "brain/telos/" in joined
    assert "brain/notes/" in joined
    assert "**/sensitive/**" in SENSITIVE_FLOOR_GLOBS


def test_build_config_from_dict_matches_load(tmp_path: Path) -> None:
    import tomllib

    raw = tomllib.loads(MINIMAL)
    assert isinstance(build_config(raw), Config)
    assert build_config(raw).sha256 == load_config(write(tmp_path, "j.toml", MINIMAL), use_local=False).sha256


def test_tmp_cfg_points_everything_at_tmp_path(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    assert Path(tmp_cfg.paths.root).is_relative_to(tmp_path)
    assert Path(tmp_cfg.paths.queue).is_dir()
    assert Path(tmp_cfg.paths.logs).is_dir()
    assert Path(tmp_cfg.daemon.state_dir).is_dir()
    assert Path(tmp_cfg.paths.vault_write_raw) == tmp_vault / "raw" / "jarvis"
    assert tmp_cfg.digest.repos == []
    assert tmp_cfg.claude.model == "sonnet"


def test_tmp_vault_layout_and_canary(tmp_vault: Path) -> None:
    assert (tmp_vault / "telos" / "sensitive" / "canary.md").read_text(encoding="utf-8").count("JARVIS-CANARY-7f3a") == 1
    for rel in ("telos", "notes", "raw/jarvis", "sessions", "session-checkpoints/processed",
                "session-checkpoints/from-old-machine"):
        assert (tmp_vault / rel).is_dir(), rel
    assert (tmp_vault / "RECENT.md").is_file()


def test_fake_clock(clock) -> None:  # noqa: ANN001
    t0 = clock()
    assert clock.advance(minutes=5) - t0 == __import__("datetime").timedelta(minutes=5)
    with pytest.raises(ValueError):
        clock.set(__import__("datetime").datetime(2026, 1, 1))


def test_daemon_code_has_no_write_path_to_jarvis_toml() -> None:
    """The config is human-owned. No module may write to it (grep, not proof)."""
    write_tokens = re.compile(
        r"write_text|write_bytes|atomic_write|os\.replace|os\.rename|shutil\.(copy|move)|"
        r"open\([^)]*['\"][wax+]"
    )
    offenders: list[str] = []
    for py in sorted((ROOT / "jarvisd").rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        if py.name == "config.py" and write_tokens.search(text):
            offenders.append(f"{py.name}: write call in the config loader")
        for n, line in enumerate(text.splitlines(), 1):
            if "jarvis.toml" in line or "jarvis.local.toml" in line:
                if write_tokens.search(line):
                    offenders.append(f"{py.name}:{n}")
    assert offenders == []


def test_watchdog_self_test_still_passes_after_append() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "watchdog.py"), "--self-test"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[FAIL]" not in proc.stdout
    assert re.search(r"self-test: (\d+)/\1 checks passed", proc.stdout)


@pytest.mark.parametrize("local, needle", [
    ('[gates]\nsensitive_term = ["acme"]\n', "gates.sensitive_term"),
    ('[gates]\nsensitive_path_glob = ["**/x/**"]\n', "gates.sensitive_path_glob"),
    ('[gate]\nsensitive_terms = ["acme"]\n', "gate"),
    ('[paths]\nvault_forbiden = ["C:/x"]\n', "paths.vault_forbiden"),
    ('[router]\nadaptor = "stub"\n', "router.adaptor"),
])
def test_local_override_typos_in_phase0_tables_fail_loudly(tmp_path: Path, local: str, needle: str) -> None:
    # [gates] and the other Phase 0 tables ignore unknown keys in the tracked file (other
    # programs own them). The private local file has one author, so a typo there is an error.
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", local)
    with pytest.raises(ConfigError, match=re.escape(needle)):
        load_config(base)


def test_local_override_error_never_echoes_values(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", '[gates]\nsensitive_term = ["very-private-name"]\n')
    with pytest.raises(ConfigError) as err:
        load_config(base)
    assert "very-private-name" not in str(err.value)


def test_tracked_file_keeps_ignoring_keys_owned_by_other_programs(tmp_path: Path) -> None:
    text = MINIMAL + '\n[watchdog]\npoll_seconds = 20\n[gates.extra]\nx = 1\n'
    load_config(write(tmp_path, "jarvis.toml", text), use_local=False)


# --- generic paths: "~" and repo-relative values (publishing, task P1) -----------------------------


def _load_watchdog():  # noqa: ANN202
    import importlib.util

    spec = importlib.util.spec_from_file_location("jarvis_watchdog_under_test", ROOT / "bin" / "watchdog.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(home))
    return home


def test_expand_path_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvisd.config import expand_path

    home = _fake_home(tmp_path, monkeypatch)
    base = tmp_path / "repo"
    assert expand_path("~", base) == home
    assert expand_path("~/brain/raw/jarvis", base) == home / "brain" / "raw" / "jarvis"
    assert expand_path("~\\brain", base) == home / "brain"
    assert expand_path(".", base) == base
    assert expand_path("queue", base) == base / "queue"
    assert expand_path("deploy/notify-jarvis.ps1", base) == base / "deploy" / "notify-jarvis.ps1"
    assert expand_path("../sibling", base) == tmp_path / "sibling"
    assert expand_path(str(tmp_path / "abs" / "x"), base) == tmp_path / "abs" / "x"


def test_load_config_expands_every_path_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    text = MINIMAL.split("[paths]")[0] + """
[paths]
root = "."
queue = "queue"
logs = "logs"
models = "models"
vault_write_raw = "~/brain/raw/jarvis"
vault_write_sessions = "~/brain/sessions"
vault_forbidden = ["~/brain/telos", "~/brain/notes"]

[daemon]
state_dir = "state"

[notify]
toast_script = "deploy/notify-jarvis.ps1"
"""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = load_config(write(repo, "jarvis.toml", text), use_local=False)
    assert cfg.paths.root == repo and cfg.paths.queue == repo / "queue"
    assert cfg.paths.logs == repo / "logs" and cfg.paths.models == repo / "models"
    assert cfg.paths.vault_write_raw == home / "brain" / "raw" / "jarvis"
    assert cfg.paths.brain_root == home / "brain"
    assert cfg.paths.vault_forbidden == [home / "brain" / "telos", home / "brain" / "notes"]
    assert cfg.daemon.state_dir == repo / "state"
    assert cfg.notify.toast_script == repo / "deploy" / "notify-jarvis.ps1"
    assert re.fullmatch(r"[0-9a-f]{64}", cfg.sha256)


def test_local_file_paths_expand_too_and_repos_may_use_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", """
[digest]
repos = [
  { name = "a", path = "~/Dev/a", work = false, counts_only = false },
  { name = "b", path = "sibling/b", work = false, counts_only = true },
]
""")
    cfg = load_config(base)
    assert [r.path for r in cfg.digest.repos] == [home / "Dev" / "a", tmp_path / "sibling" / "b"]


def test_local_vault_forbidden_appends_and_cannot_remove(tmp_path: Path) -> None:
    # Same rule as the [gates] lists: the private file can fence off more, never less.
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", '[paths]\nvault_forbidden = ["C:/x/work"]\n')
    cfg = load_config(base)
    assert cfg.paths.vault_forbidden == [Path("C:/x/brain/telos"), Path("C:/x/work")]
    write(tmp_path, "jarvis.local.toml", "[paths]\nvault_forbidden = []\n")
    assert load_config(base).paths.vault_forbidden == [Path("C:/x/brain/telos")]


def test_local_meta_carries_the_machine_identity(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", '[meta]\nmachine = "box"\nowner_account = "someone"\n')
    cfg = load_config(base)
    assert (cfg.meta.machine, cfg.meta.owner_account) == ("box", "someone")


def test_tracked_toml_is_machine_agnostic() -> None:
    import tomllib

    raw = tomllib.loads(REAL_TOML.read_text(encoding="utf-8"))
    drive = re.compile(r"^[A-Za-z]:[\/]")
    offenders: list[str] = []

    def walk(node: object, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{where}.{key}" if where else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{where}[{index}]")
        elif isinstance(node, str) and drive.match(node):
            offenders.append(f"{where} = {node!r}")

    walk(raw, "")
    assert offenders == []
    assert raw["meta"]["machine"] == "your-machine"
    assert raw["meta"]["owner_account"] == "your-account"


def test_tracked_toml_resolves_to_this_checkout_and_the_home_vault() -> None:
    cfg = load_config(REAL_TOML, use_local=False)
    assert cfg.paths.root == ROOT
    assert cfg.paths.queue == ROOT / "queue" and cfg.paths.logs == ROOT / "logs"
    assert cfg.paths.models == ROOT / "models" and cfg.daemon.state_dir == ROOT / "state"
    assert cfg.notify.toast_script == ROOT / "deploy" / "notify-jarvis.ps1"
    assert cfg.paths.brain_root == Path.home() / "brain"
    assert cfg.paths.vault_write_raw == Path.home() / "brain" / "raw" / "jarvis"
    assert Path.home() / "brain" / "telos" in cfg.paths.vault_forbidden
    assert Path.home() / "brain" / "notes" in cfg.paths.vault_forbidden


def test_watchdog_expands_the_same_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    wd = _load_watchdog()
    cfg = wd.load_config()
    assert Path(cfg["paths"]["root"]) == ROOT
    assert Path(cfg["paths"]["logs"]) == ROOT / "logs"
    assert Path(cfg["paths"]["queue"]) == ROOT / "queue"
    assert Path(cfg["paths"]["vault_write_raw"]) == Path.home() / "brain" / "raw" / "jarvis"
    assert all(Path(p).is_absolute() for p in cfg["paths"]["vault_forbidden"])
    assert Path(cfg["killswitch"]["audit_log"]) == ROOT / "logs" / "killswitch.jsonl"


def test_watchdog_self_test_passes_on_a_fresh_clone(tmp_path: Path) -> None:
    # A clone has no logs/, queue/ or models/ (all gitignored). The self-test must make them,
    # not fail on them, or a stranger's first command is a red FAIL line.
    import shutil

    clone = tmp_path / "clone"
    (clone / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "watchdog.py", clone / "bin" / "watchdog.py")
    shutil.copy(ROOT / "bin" / "kill-switch.ps1", clone / "bin" / "kill-switch.ps1")
    shutil.copy(REAL_TOML, clone / "jarvis.toml")
    assert not (clone / "logs").exists()
    proc = subprocess.run(
        [sys.executable, str(clone / "bin" / "watchdog.py"), "--self-test"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[FAIL]" not in proc.stdout
    for name in ("logs", "queue", "models"):
        assert (clone / name).is_dir()


def test_the_example_local_file_loads_on_top_of_the_tracked_config(tmp_path: Path) -> None:
    # The example is what a new user copies; it must stay a valid override of the tracked file.
    import shutil

    shutil.copy(REAL_TOML, tmp_path / "jarvis.toml")
    shutil.copy(ROOT / "jarvis.local.toml.example", tmp_path / "jarvis.local.toml")
    cfg = load_config(tmp_path / "jarvis.toml")
    assert cfg.meta.machine == "my-pc"
    assert [r.name for r in cfg.digest.repos] == ["example-api", "example-notes"]
    assert Path.home() / "Documents" / "example-employer" in cfg.paths.vault_forbidden
    assert Path.home() / "brain" / "telos" in cfg.paths.vault_forbidden


# --- keys renamed in 1.1.0 ----------------------------------------------------------------------
# The repo flag and the metadata switch used to carry the name of the owner's employer. A
# jarvis.local.toml written before the rename must keep loading, or the daemon would sit in
# config_invalid after the upgrade. The old spellings are held as hashes, so no tracked file
# spells them; they are rebuilt here from two halves for the same reason.

_OLD = "ag" + "el"


def test_a_local_file_with_the_pre_1_1_names_still_loads_under_the_new_ones(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", (
        f"[digest]\n{_OLD}_metadata_to_claude = true\n"
        f'repos = [{{ name = "r1", path = "{tmp_path.as_posix()}", {_OLD} = true }},'
        f' {{ name = "r2", path = "{tmp_path.as_posix()}", {_OLD} = false, counts_only = true }}]\n'))
    cfg = load_config(base)
    assert cfg.digest.work_metadata_to_claude is True
    assert [(r.name, r.work, r.counts_only) for r in cfg.digest.repos] == [("r1", True, False), ("r2", False, True)]


def test_the_old_router_bucket_name_in_a_local_file_maps_to_the_new_one(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", f'[router.stub.rules]\n{_OLD}_prod = ["(?i)deploy"]\n')
    assert load_config(base).router.stub.rules.work_prod == ["(?i)deploy"]


def test_both_spellings_at_once_is_an_error_not_a_silent_pick(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml",
          f"[digest]\n{_OLD}_metadata_to_claude = true\nwork_metadata_to_claude = false\n")
    with pytest.raises(ConfigError, match="both"):
        load_config(base)


def test_an_unknown_key_is_still_rejected(tmp_path: Path) -> None:
    base = write(tmp_path, "jarvis.toml", MINIMAL)
    write(tmp_path, "jarvis.local.toml", "[digest]\nbogus_metadata_to_claude = true\n")
    with pytest.raises(ConfigError, match="bogus"):
        load_config(base)
