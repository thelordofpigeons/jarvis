"""jarvisd: the JARVIS resident daemon (observe-only morning digest and its read-only companions)."""
from __future__ import annotations

from pathlib import Path

__version__ = "1.1.0"

# Repo root, resolved from this file so it works from any cwd and under pythonw.
ROOT: Path = Path(__file__).resolve().parent.parent
