# Contributing

Small, careful changes are welcome. This is a personal project, so expect slow and blunt reviews.

- Write the test first, watch it fail, then implement. Run `python -m pytest -q` from the repo root.
- Keep dependencies at stdlib, pydantic, APScheduler and pytest, plus fastapi, uvicorn and httpx2 for the read-only hub only (the `hub` extra in `pyproject.toml`, pinned in `requirements.lock`); anything new needs a one-line justification in the docs.
- Privacy rules are absolute: the tier gate runs before any read, one writer touches the vault, Claude is reached only through the isolated argv in `jarvisd/claude.py`, and every new Claude use goes through the budget ledger and breaker.
- Every new action writes an audit event (ids, counts and hashes, never text).
- Windows first: `pathlib`, `os.replace`, list argv, no `shell=True`, UTF-8 without BOM, LF line endings.
- Style: `from __future__ import annotations`, type hints, `main(argv) -> int`, comments say why. No em or en dashes anywhere.
- Never commit a name, path, address or token of a real person or machine; `tests/test_repo_hygiene.py` checks, and your private values belong in the gitignored `jarvis.local.toml`.
- Say plainly in the docs what is tested, what is only an adapter and what is not built.
- Do not run `jarvis run-digest --claude` or other paid calls in tests; use the fake binary in `tests/fakes/`.
- Open an issue before a large change. Security problems: open an issue titled "security" without details and the author will reply.
