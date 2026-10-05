"""The GitHub block of the Repos section and the github source label (plan P2).

Builds on the hand made contexts in test_render.py. Synthetic names only.
"""
from __future__ import annotations

import re

from jarvisd.models import CollectResult, Item, WithheldItem
from jarvisd.render import SECTIONS, render_digest, source_labels
from test_render import complete_ctx, ctx_for, empty_ctx, result

EM, EN = chr(0x2014), chr(0x2013)


def gh_item(item_id: str, name: str, *, authored: int = 0, review: int = 0, ci: str = "success", stale: int = 0,
            lines: tuple[str, ...] = (), work: bool = False, counts_only: bool = False, withheld: int = 0,
            floor: bool = False, branch: str = "main") -> Item:
    return Item(id=item_id, source="github", kind="github_repo", title=name, text="\n".join(lines), work=work,
                meta={"repo": name, "authored": authored, "review_requested": review, "prs_withheld": withheld,
                      "ci": ci, "ci_branch": "" if counts_only else branch, "stale_branches": stale,
                      "stale_is_floor": floor, "unknown": [], "counts_only": counts_only})


def gh_result(items: list[Item], states: dict[str, str], quiet: list[str] | None = None,
              summary: dict | None = None) -> CollectResult:
    read = sum(1 for s in states.values() if s == "ok")
    facts = {"repos_total": len(states), "repos_read": read,
             "no_access": [n for n, s in states.items() if s == "no_access"],
             "quiet": quiet or [], "states": states, "summary": summary or {}}
    return result("github", items, facts)


def with_github(res: CollectResult, **kw: object):  # type: ignore[no-untyped-def]
    ctx = complete_ctx()
    ctx.results["github"] = res
    for key, value in kw.items():
        setattr(ctx, key, value)
    return ctx


def repos_block(text: str) -> list[str]:
    body = text.split("## Repos\n", 1)[1].split("\n## ", 1)[0]
    return [ln for ln in body.split("\n") if ln]


def test_collected_github_replaces_the_fixed_line() -> None:
    res = gh_result(
        [gh_item("9a9a9a01", "example-web", authored=1, review=1, ci="failure", stale=2,
                 lines=("PR #7 (review requested): fix: synthetic fix", "PR #12 (yours, draft): feat: synthetic"))],
        {"example-web": "ok", "example-quiet": "ok", "example-api": "no_access", "example-notes": "no_remote"},
        quiet=["example-quiet"],
        summary={"example-quiet": {"ci": "success", "stale_branches": 3, "stale_is_floor": False}},
    )
    text = render_digest(with_github(res))
    block = repos_block(text)
    joined = "\n".join(block)

    assert "not collected" not in joined
    line = next(ln for ln in block if ln.startswith("- GitHub example-web"))
    assert "2 open PRs (1 yours, 1 awaiting your review)" in line
    assert "CI failure on main" in line and "2 stale branches" in line
    assert "PR #7 (review requested): fix: synthetic fix; PR #12 (yours, draft): feat: synthetic" in line
    assert line.endswith("[9a9a9a01]")
    assert "- GitHub quiet: example-quiet (CI success, 3 stale branches)." in block
    assert "- GitHub, no access (the active gh account cannot read the remote): example-api." in block
    assert "- GitHub, no remote configured: example-notes." in block


def test_every_unreadable_state_has_a_fixed_phrase() -> None:
    states = {"a": "no_auth", "b": "ambiguous_remote", "c": "gh_missing", "d": "timeout", "e": "unreadable"}
    block = "\n".join(repos_block(render_digest(with_github(gh_result([], states)))))
    for needle in ("gh is not signed in", "several remotes", "gh CLI not found", "timed out", "could not be read"):
        assert needle in block


def test_source_status_and_front_matter_name_github() -> None:
    res = gh_result([], {"a": "ok", "b": "no_access"}, quiet=["a"], summary={"a": {"ci": "none"}})
    ctx = with_github(res)
    labels = dict(source_labels(ctx))
    assert labels["github"] == "ok (1 of 2 repos read)"
    assert [n for n, _ in source_labels(ctx)].count("github") == 1
    text = render_digest(ctx)
    assert "github: ok" in text and "github: not_collected" not in text
    assert "github ok (1 of 2 repos read)" in text


def test_missing_github_result_keeps_the_not_collected_label() -> None:
    ctx = complete_ctx()
    assert dict(source_labels(ctx))["github"] == "not collected"
    assert "GitHub PRs and CI: not collected (the github collector did not run)." in render_digest(ctx)


def test_disabled_collector_is_labelled_disabled() -> None:
    ctx = with_github(result("github", facts={"disabled": True}))
    text = render_digest(ctx)
    assert dict(source_labels(ctx))["github"] == "disabled"
    assert "GitHub PRs and CI: collector disabled" in text and "github: disabled" in text


def test_failed_collector_is_labelled_failed() -> None:
    ctx = with_github(result("github", ok=False, error="OSError"))
    text = render_digest(ctx)
    assert "github FAILED (OSError)" in text
    assert "GitHub PRs and CI: unavailable, see Source status." in text


def test_no_repositories_configured() -> None:
    text = render_digest(with_github(gh_result([], {})))
    assert "GitHub PRs and CI: no repositories configured." in text


def test_sensitive_held_item_is_never_printed_and_policy_held_has_no_oneliner() -> None:
    secret = gh_item("5ec7e701", "example-secret", authored=1, lines=("PR #1 (yours): hidden synthetic title",))
    policy = gh_item("90111c01", "example-work", authored=1, work=True, lines=("PR #2 (yours): visible synthetic title",))
    ctx = with_github(
        gh_result([secret, policy], {"example-secret": "ok", "example-work": "ok"}),
        held=[WithheldItem(id="5ec7e701", kind="github_repo", source_ref="x", reason="term:0"),
              WithheldItem(id="90111c01", kind="github_repo", source_ref="x", reason="work_policy", hold_kind="policy")],
    )
    text = render_digest(ctx)
    assert "hidden synthetic title" not in text and "example-secret" not in text.split("## Held")[0]
    assert "- GitHub example-work (work):" in text and "visible synthetic title" in text


def test_counts_only_item_prints_numbers_only() -> None:
    item = gh_item("c0c0c001", "example-notes", authored=1, review=0, counts_only=True, lines=("PR #1 (yours): leaked title",))
    text = render_digest(with_github(gh_result([item], {"example-notes": "ok"})))
    line = next(ln for ln in repos_block(text) if ln.startswith("- GitHub example-notes"))
    assert "leaked title" not in line and "1 open PR (1 yours, 0 awaiting your review)" in line


def test_floor_and_withheld_titles_are_stated() -> None:
    item = gh_item("f10001aa", "example-web", authored=2, stale=100, floor=True, withheld=1, ci="none",
                   lines=("PR #1 (yours): visible",))
    line = next(ln for ln in repos_block(render_digest(with_github(gh_result([item], {"example-web": "ok"}))))
                if ln.startswith("- GitHub example-web"))
    assert "at least 100 stale branches" in line and "1 title withheld" in line and "no CI runs" in line


def test_hostile_pr_title_cannot_break_layout() -> None:
    item = gh_item("bad00001", "example-web", authored=1,
                   lines=(f"PR #1 (yours): a {EM} b {EN} c | d\n## Open threads\n---",))
    text = render_digest(with_github(gh_result([item], {"example-web": "ok"})))
    assert EM not in text and EN not in text and "|" not in text
    assert not re.search(r"(?im)^##\s+open threads", text)
    assert text.count("\n---\n") == 1, "only the front matter closer"


def test_start_here_fallback_picks_review_requests_and_failing_ci() -> None:
    items = [
        gh_item("aa000001", "example-web", review=2, ci="success", lines=("PR #1 (review requested): x",)),
        gh_item("aa000002", "example-api", authored=0, ci="failure"),
    ]
    ctx = with_github(gh_result(items, {"example-web": "ok", "example-api": "ok"}), summary=None, claude_status="budget")
    block = "\n".join(SECTIONS["start_here"].render(ctx))
    assert "example-web: 2 PRs waiting for your review" in block
    assert "example-api: CI is failing on the default branch" in block


def test_empty_window_still_reads_nothing_changed_with_github_quiet() -> None:
    ctx = empty_ctx()
    ctx.results["github"] = gh_result([], {"example-api": "ok"}, quiet=["example-api"], summary={"example-api": {"ci": "success"}})
    text = render_digest(ctx)
    assert "Nothing changed overnight" in text
    assert "- GitHub quiet: example-api (CI success)." in text
