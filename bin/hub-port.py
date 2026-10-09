"""Exit with the hub's port ([hub].port, jarvis.local.toml merged) so a .cmd can read it from %errorlevel%.

bin\\hub.cmd and bin\\face-window.cmd call this instead of `python -c "..."` inside a `for /f`: cmd's quote
handling in that construct breaks on the parentheses of any Python expression. An exit code carries a port
(1024 to 65535) without a single quote; on any failure the exit code is 0 and the caller falls back to 8765.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from jarvisd.config import load_config

    port = int(load_config().hub.port)
    sys.exit(port if 1024 <= port <= 65535 else 0)
except SystemExit:
    raise
except BaseException:  # noqa: BLE001  any problem means "use the default"
    sys.exit(0)
