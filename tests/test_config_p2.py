"""Config keys added by plan P2: [digest.github] and the ntfy keys of [notify]."""
from __future__ import annotations

from pathlib import Path

import pytest

from jarvisd import ROOT
from jarvisd.config import Config, ConfigError, load_config


def test_tracked_toml_turns_the_github_collector_on_with_the_documented_values() -> None:
    cfg = load_config(ROOT / "jarvis.toml", use_local=False)
    assert cfg.digest.github.enabled is True
    assert cfg.digest.github.max_prs == 10
    assert cfg.digest.github.timeout_s == 20


def test_github_defaults_fail_closed_when_the_table_is_absent(tmp_cfg: Config) -> None:
    from jarvisd.config import GithubCfg

    assert GithubCfg().enabled is False


@pytest.mark.parametrize("snippet", [
    "[digest.github]\nenabled = true\nmax_prz = 3\n",
    "[digest.github]\nenabled = \"yes\"\n",
    "[digest.github]\nmax_prs = 0\n",
    "[digest.github]\nmax_prs = 500\n",
    "[digest.github]\ntimeout_s = 0\n",
])
def test_bad_github_keys_fail_loudly(tmp_path: Path, snippet: str) -> None:
    base = (ROOT / "jarvis.toml").read_text(encoding="utf-8")
    cut = base.index("[digest.github]")
    bad = tmp_path / "jarvis.toml"
    bad.write_text(base[:cut] + snippet, encoding="utf-8", newline="\n")
    with pytest.raises(ConfigError):
        load_config(bad, use_local=False)


def test_local_file_can_switch_github_off(tmp_path: Path) -> None:
    base = tmp_path / "jarvis.toml"
    base.write_text((ROOT / "jarvis.toml").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    (tmp_path / "jarvis.local.toml").write_text("[digest.github]\nenabled = false\nmax_prs = 3\n", encoding="utf-8", newline="\n")
    cfg = load_config(base)
    assert cfg.digest.github.enabled is False and cfg.digest.github.max_prs == 3


def test_notify_ntfy_defaults(tmp_cfg: Config) -> None:
    n = tmp_cfg.notify
    assert n.adapter == "toast"
    assert (n.ntfy_url, n.ntfy_topic, n.ntfy_token_env) == ("", "", "")
    assert n.ntfy_priority == 3 and n.ntfy_timeout_s == 10
    assert "{path}" in n.ntfy_click_template


def _with_notify(tmp_path: Path, body: str) -> Path:
    base = tmp_path / "jarvis.toml"
    base.write_text((ROOT / "jarvis.toml").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    (tmp_path / "jarvis.local.toml").write_text("[notify]\n" + body, encoding="utf-8", newline="\n")
    return base


def test_local_file_sets_the_ntfy_keys(tmp_path: Path) -> None:
    cfg = load_config(_with_notify(tmp_path, (
        'adapter = "ntfy"\nntfy_url = "https://ntfy.example.invalid:2586"\nntfy_topic = "jarvis-digest"\n'
        'ntfy_token_env = "JARVIS_NTFY_TOKEN"\nntfy_priority = 4\n')))
    assert cfg.notify.adapter == "ntfy" and cfg.notify.ntfy_topic == "jarvis-digest"
    assert cfg.notify.ntfy_token_env == "JARVIS_NTFY_TOKEN" and cfg.notify.ntfy_priority == 4


@pytest.mark.parametrize("body", [
    'ntfy_priority = 0\n',
    'ntfy_priority = 6\n',
    'ntfy_topic = "has spaces"\n',
    'ntfy_topic = "../escape"\n',
    'ntfy_token_env = "not a var name"\n',
    'ntfy_url = "ftp://example.invalid"\n',
    'ntfy_url = "file:///etc/passwd"\n',
    'ntfy_url = "http://user:pw@example.invalid"\n',
    'ntfy_url = "http://192.168.1.20"\n',
    'ntfy_url = "http://ntfy.example.invalid:2586"\n',
    'ntfy_timeout_s = 0\n',
    'ntfy_unknown = 1\n',
])
def test_bad_ntfy_keys_fail_loudly(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_with_notify(tmp_path, body))
