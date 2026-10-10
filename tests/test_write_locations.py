"""AST scan: only the named modules write files, only vault.py writes to the vault root.

Why an AST scan and not a grep: a grep misses `from os import replace` and aliasing, and a
list of allowed modules is only worth anything if a new module that quietly opens a file for
writing turns this test red (design sections 9 and 14).
"""
from __future__ import annotations

import ast
from pathlib import Path

from jarvisd import ROOT

PKG = ROOT / "jarvisd"

# Modules that may open files for writing (design section 14). claude.py is the opt-in
# payload archive, cli.py is corrections, the rest are durable IO behind small classes.
WRITERS = frozenset({"vault.py", "audit.py", "jobstore.py", "state.py", "fsio.py", "claude.py", "cli.py"})
# __main__.py is not in the design list but is the process entry point: it opens
# logs/jarvisd-fault.log and logs/jarvisd-crash.log because pythonw has no console to print
# to. test_main_writes_only_crash_logs pins that exception so it cannot grow.
ENTRY_POINT = "__main__.py"

# Config attributes that name the two writable vault locations. Only the writer and the
# config model that declares them may mention them.
VAULT_WRITE_ATTRS = frozenset({"vault_write_raw", "vault_write_sessions"})
VAULT_ATTR_OWNERS = frozenset({"vault.py", "config.py"})

_WRITE_MODE_CHARS = set("wax+")
_WRITE_FLAG_NAMES = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC", "O_EXCL"}
_DESTRUCTIVE_ATTRS = {
    ("os", "replace"), ("os", "rename"), ("os", "renames"), ("os", "truncate"),
    ("shutil", "move"), ("shutil", "copy"), ("shutil", "copy2"), ("shutil", "copyfile"),
    ("shutil", "copytree"),
}
# Path.replace and Path.rename are not listed: str.replace is everywhere and cannot be told
# apart without types. os.replace, os.rename and shutil are covered above.
_PATH_WRITE_METHODS = {"write_text", "write_bytes", "touch"}
_MODULE_OF_DESTRUCTIVE = {"os", "shutil"}


def _mode_is_write(node: ast.expr | None) -> bool:
    """True for a mode that can write, and for any mode we cannot read statically."""
    if node is None:
        return False  # open(path) defaults to "r"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(_WRITE_MODE_CHARS & set(node.value))
    return True  # computed mode: cannot prove it is read-only


def _call_mode(call: ast.Call, positional_index: int) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value
    if len(call.args) > positional_index:
        return call.args[positional_index]
    return None


def _flags_write(call: ast.Call) -> bool:
    names = {n.attr if isinstance(n, ast.Attribute) else n.id
             for arg in [*call.args, *(kw.value for kw in call.keywords)]
             for n in ast.walk(arg) if isinstance(n, (ast.Attribute, ast.Name))}
    return bool(_WRITE_FLAG_NAMES & names)


def write_findings(source: str) -> list[str]:
    """Every construct in source that writes a file or moves one. Pure, so it is testable."""
    tree = ast.parse(source)
    # `from os import replace as r` and `from shutil import move` bind a bare name.
    bare: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _MODULE_OF_DESTRUCTIVE:
            for alias in node.names:
                if (node.module, alias.name) in _DESTRUCTIVE_ATTRS:
                    bare[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        where = f"line {node.lineno}"
        if isinstance(func, ast.Name):
            if func.id == "open" and _mode_is_write(_call_mode(node, 1)):
                found.append(f"{where}: open() for writing")
            elif func.id in bare:
                found.append(f"{where}: {bare[func.id]}")
        elif isinstance(func, ast.Attribute):
            owner = func.value.id if isinstance(func.value, ast.Name) else None
            if (owner, func.attr) in _DESTRUCTIVE_ATTRS:
                found.append(f"{where}: {owner}.{func.attr}")
            elif owner == "io" and func.attr == "open" and _mode_is_write(_call_mode(node, 1)):
                found.append(f"{where}: io.open() for writing")
            elif owner == "os" and func.attr == "open" and _flags_write(node):
                found.append(f"{where}: os.open() with write flags")
            elif owner == "os" and func.attr == "fdopen" and _mode_is_write(_call_mode(node, 1)):
                found.append(f"{where}: os.fdopen() for writing")
            elif func.attr == "open" and owner not in {"os", "io"} and _mode_is_write(_call_mode(node, 0)):
                found.append(f"{where}: Path.open() for writing")
            elif func.attr in _PATH_WRITE_METHODS and owner not in {"os", "shutil"}:
                found.append(f"{where}: .{func.attr}()")
    return found


def vault_attr_findings(source: str) -> list[str]:
    """References to the two writable vault locations of the config."""
    return [f"line {n.lineno}: .{n.attr}" for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Attribute) and n.attr in VAULT_WRITE_ATTRS]


def _modules() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _label(path: Path) -> str:
    return path.relative_to(PKG).as_posix()


def test_only_named_modules_write_files() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _modules():
        label = _label(path)
        if path.name in WRITERS and path.parent == PKG:
            continue
        if label == ENTRY_POINT:
            continue
        hits = write_findings(path.read_text(encoding="utf-8"))
        if hits:
            offenders[label] = hits
    assert not offenders, f"modules outside the writer list touch files for writing: {offenders}"


def test_only_vault_references_the_vault_write_roots() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _modules():
        if path.name in VAULT_ATTR_OWNERS and path.parent == PKG:
            continue
        hits = vault_attr_findings(path.read_text(encoding="utf-8"))
        if hits:
            offenders[_label(path)] = hits
    assert not offenders, f"only vault.py may use the vault write roots: {offenders}"


def test_main_writes_only_crash_logs() -> None:
    """The entry point may open exactly its two log files and replace or move nothing."""
    source = (PKG / ENTRY_POINT).read_text(encoding="utf-8")
    hits = write_findings(source)
    assert all("open() for writing" in h for h in hits), hits
    assert len(hits) <= 2, hits
    assert "brain" not in source and not vault_attr_findings(source)
    assert "jarvisd-fault.log" in source and "jarvisd-crash.log" in source


def test_the_scan_notices_each_kind_of_write() -> None:
    """Prove the scanner is not blind: every bypass below must be reported."""
    bad = [
        "open('x', 'w')",
        "open('x', mode='a')",
        "open('x', 'rb+')",
        "open('x', mode)",
        "import io\nio.open('x', 'w')",
        "import os\nos.replace('a', 'b')",
        "import os\nos.rename('a', 'b')",
        "import os\nos.open('x', os.O_WRONLY | os.O_CREAT)",
        "import shutil\nshutil.move('a', 'b')",
        "from shutil import move as m\nm('a', 'b')",
        "from os import replace\nreplace('a', 'b')",
        "from pathlib import Path\nPath('x').write_text('t')",
        "from pathlib import Path\np = Path('x')\np.write_bytes(b'')",
        "from pathlib import Path\nPath('x').open('w')",
        "from pathlib import Path\nPath('x').open(mode='a')",
    ]
    for source in bad:
        assert write_findings(source), source


def test_the_scan_lets_reads_through() -> None:
    good = [
        "open('x')",
        "open('x', 'r')",
        "open('x', 'rb')",
        "import os\nos.open('x', os.O_RDONLY)",
        "from pathlib import Path\nPath('x').read_text()",
        "from pathlib import Path\nPath('x').open('rb')",
        "import os\nos.path.exists('x')",
    ]
    for source in good:
        assert write_findings(source) == [], source


def test_the_vault_attribute_scan_notices_a_reference() -> None:
    assert vault_attr_findings("def f(cfg):\n    return cfg.paths.vault_write_raw\n")
    assert vault_attr_findings("x = cfg.paths.vault_write_sessions / 'a'")
    assert not vault_attr_findings("x = cfg.paths.brain_root")


# --- collectors read only through tier.safe_read_text (T8) ----------------------------

# Any of these in jarvisd/collectors/ is a way around the tier gate's read primitive.
_READ_METHODS = {"read_text", "read_bytes", "open", "readlines", "readline"}


def collector_read_findings(source: str) -> list[str]:
    """Calls that read file content directly. Directory listings and stat() are fine."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in {"open", "fdopen"}:
            found.append(f"line {node.lineno}: {func.id}()")
        elif isinstance(func, ast.Attribute) and func.attr in _READ_METHODS:
            found.append(f"line {node.lineno}: .{func.attr}()")
    return found


def test_collectors_do_not_read_files_directly() -> None:
    collectors = sorted((PKG / "collectors").glob("*.py"))
    assert {p.name for p in collectors} >= {"__init__.py", "brain.py", "task.py", "git.py", "system.py"}
    offenders = {p.name: hits for p in collectors if (hits := collector_read_findings(p.read_text(encoding="utf-8")))}
    assert not offenders, f"collectors must read through tier.safe_read_text only: {offenders}"


def test_collector_read_scan_catches_the_obvious_bypasses() -> None:
    assert collector_read_findings("open('x')")
    assert collector_read_findings("Path('x').read_text()")
    assert collector_read_findings("p.read_bytes()")
    assert collector_read_findings("io.open(p)")
    assert not collector_read_findings("os.scandir(d); p.stat(); safe_read_text(p, cfg, roots)")


# --- the state paths of the item sidecar and history (docs/hub-rework-contract.md section 6) ------

# The only file names the digest writer may spell under state/: everything sits in runs/<job>/, and the
# history and the attention decisions are reached through StateStore.items, never by a literal path.
DIGEST_STATE_FILES = frozenset({"summary.json", "run.json", "digest-unwritten.md", "items.json"})


def _code_without_docstrings(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            del body[0]
    return ast.unparse(tree)


def test_digest_writer_names_only_the_contract_state_paths() -> None:
    import re

    from jarvisd import state

    code = _code_without_docstrings((PKG / "digest.py").read_text(encoding="utf-8"))
    names = set(re.findall(r"'([\w-]+\.(?:json|md|jsonl))'", code))
    assert names == DIGEST_STATE_FILES, names
    assert "item-history" not in code and '"attention"' not in code
    assert state.ITEM_HISTORY_FILE == "item-history.json" and state.ATTENTION_DIR == "attention"
    assert "state.py" in WRITERS and "digest.py" not in WRITERS and "weekly.py" not in WRITERS
    weekly = (PKG / "weekly.py").read_text(encoding="utf-8")
    assert write_findings(weekly) == [] and vault_attr_findings(weekly) == []
    assert "write_raw" in weekly and "raw/jarvis" not in weekly.replace("`raw/jarvis/", "")
