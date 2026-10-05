# Publishing this repository

Status: a checklist for the maintainer, enforced by tests where a test can enforce it.

A public push publishes every commit reachable from the branches you push, not only the files
you see today. The working-tree rules (`tests/test_repo_hygiene.py`) keep the files clean; this
page is about the history and about what must stay out.

## Rules

- Private values live in the gitignored `jarvis.local.toml`: repository list, sensitive terms,
  private folders, the machine and account names under `[meta]`, `[hygiene].deny_substrings`,
  `[digest].watched_tasks`, the ntfy address and topic. Nothing private is tracked.
- A tracked file names no person, employer, client, machine, tailnet, address, token, absolute
  user path, ClickUp or hosting identifier. The hygiene tests check the generic shapes (user
  profile paths, e-mail addresses, Tailscale addresses, token shapes), a hashed list of names that
  must not be spelled in a public file, and your own names from `jarvis.local.toml` and the
  environment.
- The history obeys the same rules. `test_git_history_has_no_identity_leaks` walks every commit
  reachable from the branch you have checked out: author and committer addresses must be GitHub noreply addresses, and every
  message and every line ever added is scanned like a tracked file. A line that was added in one
  commit and removed in the next still fails, because it is still in the history.

## Publishing from a clean history

If the history already contains a leak, do not try to scrub it commit by commit. Publish from a
single fresh commit instead:

```powershell
git branch legacy-history                 # local only, never pushed; delete it once you no longer need it
git checkout --orphan main
git add -A                                # the gitignore keeps state, logs and the local config out
git -c user.name="<name>" -c user.email="<id>+<handle>@users.noreply.github.com" -c core.autocrlf=false commit -m "<message>"
git branch -D master                      # or leave it; it is never pushed
.venv\Scripts\python.exe -m pytest -q tests\test_repo_hygiene.py
git remote add origin https://github.com/<handle>/jarvis.git
git push -u origin main                   # never --all, never --mirror
```

Use the noreply address that GitHub shows under Settings, Emails, and turn on "Keep my email
addresses private" and "Block command line pushes that expose my email" there too. Do not push
the `legacy-history` branch: it still carries the old identities, and the history test would fail
on it if you checked it out. Push `main` by name, as above.

## Before every release

1. `python -m pytest -q` is green, including the history test on the branch you will push.
2. `python bin/watchdog.py --self-test` and `python -m jarvisd self-test` pass.
3. `git log --format="%an <%ae> %cn <%ce>" | sort -u` shows only noreply addresses.
4. Read the README status table once more: every row says what was run for real and what was only
   tested against a fake.
5. Nothing in `state/`, `queue/`, `logs/` or `jarvis.local.toml` is tracked (`git ls-files` shows none).
