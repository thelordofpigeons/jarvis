"""The work hub: a read-only local cockpit over what the daemon already keeps on disk.

Entry points: `app.create_app(cfg)` builds the FastAPI application, `app.serve(cfg, port)`
runs it on 127.0.0.1, `check.run_check(cfg)` renders every view as a self-test. See docs/hub.md.
"""
from __future__ import annotations
