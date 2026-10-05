"""Entry point for `python -m jarvisd` and for pythonw under Task Scheduler.

pythonw has no console, so sys.stdout and sys.stderr are None there. Anything that prints
would raise, and an uncaught error would vanish. This module therefore routes crashes to
files under logs/ first, then hands over to the CLI.

The subcommands live in jarvisd/cli.py. An uncaught error goes to logs/jarvisd-crash.log
(full traceback) and to the audit chain as `daemon_crash` (class and location only), from the
main thread and from any other thread. If cli.py is missing, --help still answers.
"""
from __future__ import annotations

import argparse
import faulthandler
import importlib
import io
import sys
import threading
import traceback
from pathlib import Path
from typing import Sequence

from jarvisd import ROOT, __version__

SUBCOMMANDS: tuple[str, ...] = (
    "serve", "run-digest", "status", "digest", "held", "wrong", "pause", "resume",
    "audit", "breaker", "self-test", "install-task",
)

_fault_file: io.TextIOWrapper | None = None  # kept referenced for the life of the process


def _ensure_streams() -> None:
    """Give pythonw a sink for print() so nothing raises on a missing stdout."""
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()


def _record_crash(target: Path, exc_type: type[BaseException], tb: object, thread: str | None) -> None:
    """Add a `daemon_crash` record to the audit chain: class and location, never the message.

    A message can carry a path or a piece of content, and the audit holds ids and counts only.
    The full traceback goes to the crash log, which stays on this machine.
    """
    try:
        from jarvisd.audit import AuditLog

        frames = traceback.extract_tb(tb)  # type: ignore[arg-type]
        where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
        AuditLog(target / "jarvisd-audit.jsonl", mirror_stdout=False).emit(
            "daemon_crash", error_type=exc_type.__name__, where=where, thread=thread)
    except Exception:  # noqa: BLE001  the crash trail must never raise inside the crash hook
        pass


def install_crash_handlers(log_dir: Path | None = None) -> None:
    """Send uncaught exceptions and hard faults to files, because pythonw shows nothing."""
    global _fault_file
    target = log_dir if log_dir is not None else ROOT / "logs"
    try:
        target.mkdir(parents=True, exist_ok=True)
        _fault_file = open(target / "jarvisd-fault.log", "a", encoding="utf-8")
        faulthandler.enable(file=_fault_file, all_threads=True)
    except OSError:
        # Logging must never be the reason the daemon cannot start.
        _fault_file = None

    def write_trace(exc_type: type[BaseException], exc: BaseException, tb: object, label: str) -> None:
        try:
            with open(target / "jarvisd-crash.log", "a", encoding="utf-8") as fh:
                fh.write(f"[{label}]\n")
                fh.write("".join(traceback.format_exception(exc_type, exc, tb)))
                fh.write("\n")
        except OSError:
            pass

    def hook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        write_trace(exc_type, exc, tb, "main")
        _record_crash(target, exc_type, tb, None)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit or args.exc_value is None:
            return
        name = args.thread.name if args.thread is not None else None
        write_trace(args.exc_type, args.exc_value, args.exc_traceback, name or "thread")
        _record_crash(target, args.exc_type, args.exc_traceback, name)

    sys.excepthook = hook
    threading.excepthook = thread_hook


def _fallback_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvisd",
        description="JARVIS daemon, v1 option 2 (morning digest, observe-only).",
    )
    parser.add_argument("--version", action="version", version=f"jarvisd {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=SUBCOMMANDS,
        metavar="command",
        help="one of: " + ", ".join(SUBCOMMANDS) + " (not implemented yet in this build)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        cli = importlib.import_module("jarvisd.cli")
    except ModuleNotFoundError as exc:
        # Only a missing cli module falls back; a broken import inside it must surface.
        if exc.name != "jarvisd.cli":
            raise
        parsed = _fallback_parser().parse_args(args)
        if parsed.command is None:
            _fallback_parser().print_help()
            return 0
        print(f"jarvisd {parsed.command}: not implemented yet in this build", file=sys.stderr)
        return 2
    return int(cli.main(args))


if __name__ == "__main__":
    install_crash_handlers()
    sys.exit(main())
