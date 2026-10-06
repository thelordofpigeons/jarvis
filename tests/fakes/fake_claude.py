"""Stand-in for the `claude` binary, driven through the real ClaudeClient (T7).

Not a mock of the client: the client spawns this file as a real child process (through a
test runner that swaps argv[0]), so argv, env, cwd, stdin, timeouts and process-tree kills
are all exercised for real. Stdlib only.

Environment (set by the test runner, which is why they are not in the client's allowlist):
  FAKE_CLAUDE_LOG        JSONL file; one record per invocation (argv, env keys, cwd, stdin, pid)
  FAKE_CLAUDE_SCENARIO   ok | fenced | hallucinated | timeout | hang | 429 | 401 | 500 |
                         invalid_json | bad_schema | isolation_violation | breach | no_cost | inf_cost
                         ask_ok | ask_hallucinated | ask_bad_schema
                         clickup_ok | clickup_none | clickup_empty | clickup_malformed | clickup_badshape |
                         clickup_over30 | clickup_one_bad | clickup_forbidden_tool | clickup_toolsearch |
                         clickup_denial   (stream-json output, the clickup_read profile)
  FAKE_CLAUDE_CLICKUP    clickup_* variant served to a stream-json call while the scenario is "ok"
  FAKE_CLAUDE_TOUCH      file to create in the `breach` scenario (a stray Stop-hook checkpoint)
  FAKE_CLAUDE_TOUCH_AGE  seconds into the past to date that file (a Syncthing delivery keeps the
                         sender's modification time)
  FAKE_CLAUDE_MAX_TURNS  "1" makes --help advertise --max-turns
  FAKE_CLAUDE_CANDIDATES for the consolidation prompt (system prompt mentions "memory candidates"):
                         ok (default, 2 candidates) | many (12) | ungrounded (1 good, 1 invented
                         evidence) | none (empty list)
  FAKE_CLAUDE_PROPOSALS  for the proposals prompt (system prompt mentions "task proposals"):
                         ok (default, 2 proposals) | many (12) | none ([]) | outside (1 good, 1 whose
                         evidence id was never sent) | mixed (one proposal citing a sent id and an
                         unsent one) | dupes (same title twice, different spelling) | invalid_json |
                         not_a_list (an object) | held_guess (cites the id t-term) | one_bad (1 good, 1 with an unknown key) | echo_rejected
                         (cites a rejected-example id as evidence) | canary (title carries the term
                         in FAKE_CLAUDE_TERM)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "claude_result_ok.json"
CLICKUP_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "clickup_result_ok.json"
CLICKUP_TOOL = "mcp__claude_ai_ClickUp__clickup_filter_tasks"
REQUIRED_FLAGS = (
    "--output-format", "--model", "--setting-sources", "--disable-slash-commands",
    "--strict-mcp-config", "--tools", "--no-session-persistence", "--permission-prompts",
    "--max-budget-usd", "--system-prompt",
    "--allowedTools", "--disallowedTools", "--verbose",
)


def _record(stdin_text: str) -> None:
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if not path:
        return
    entry = {
        "argv": sys.argv[1:],
        "env_keys": sorted(os.environ),
        "cwd": os.getcwd(),
        "stdin": stdin_text,
        "pid": os.getpid(),
        "scenario": os.environ.get("FAKE_CLAUDE_SCENARIO", "ok"),
    }
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry) + "\n")


def _help() -> str:
    flags = list(REQUIRED_FLAGS) + ["-p", "--print"]
    if os.environ.get("FAKE_CLAUDE_MAX_TURNS") == "1":
        flags.append("--max-turns")
    return "Usage: claude [options]\n" + "\n".join(f"  {f}  synthetic option" for f in flags) + "\n"


def _ids(stdin_text: str) -> list[str]:
    block = re.search(r"<data>\n(.*)\n</data>", stdin_text, re.DOTALL)
    if not block:
        return []
    try:
        rows = json.loads(block.group(1))
    except ValueError:
        return []
    return [row["id"] for row in rows if isinstance(row, dict) and "id" in row]


def _system_prompt() -> str:
    args = sys.argv[1:]
    return args[args.index("--system-prompt") + 1] if "--system-prompt" in args else ""


def _refs(stdin_text: str) -> list[tuple[str, int]]:
    """(note, line) pairs from consolidation ids shaped 'note#L12'."""
    out = []
    for item_id in _ids(stdin_text):
        note, _, line = item_id.rpartition("#L")
        if note and line.isdigit():
            out.append((note, int(line)))
    return out


def _candidate(title: str, refs: list[tuple[str, int]]) -> dict:
    return {
        "title": title, "pattern": f"Synthetic pattern for {title}.", "why_it_matters": "Synthetic reason.",
        "applies_to": "synthetic project", "evidence": [{"note": n, "line": ln} for n, ln in refs],
    }


def _candidates_result(stdin_text: str) -> dict:
    refs = _refs(stdin_text)
    mode = os.environ.get("FAKE_CLAUDE_CANDIDATES", "ok")
    if mode == "none" or not refs:
        return {"candidates": []}
    if mode == "many":
        return {"candidates": [_candidate(f"Idea {n}", refs[:1]) for n in range(12)]}
    if mode == "ungrounded":
        return {"candidates": [_candidate("Grounded idea", refs[:1]),
                               _candidate("Invented idea", [("made-up-note", 1)])]}
    return {"candidates": [_candidate("First idea", refs[:2]), _candidate("Second idea", refs[-1:])]}


def _proposal(title: str, evidence: list[str], **over: object) -> dict:
    return {"title": title, "project": "synthetic-project", "kind": "task", "evidence": evidence,
            "suggested_status": "to do", "due_hint": None, "rationale": f"Synthetic reason for {title}.", **over}


def _proposals_result(stdin_text: str) -> object:
    all_ids = _ids(stdin_text)
    ids = [i for i in all_ids if not i.startswith("rejected-")]
    mode = os.environ.get("FAKE_CLAUDE_PROPOSALS", "ok")
    if mode == "invalid_json":
        return "this is not json ["
    if mode == "not_a_list":
        return {"proposals": []}
    if mode == "none" or not ids:
        return []
    if mode == "many":
        return [_proposal(f"Synthetic work {n}", ids[:1]) for n in range(12)]
    if mode == "outside":
        return [_proposal("Grounded work", ids[:1]), _proposal("Invented work", ["made-up-id"])]
    if mode == "mixed":
        return [_proposal("Half grounded work", [ids[0], "made-up-id"])]
    if mode == "dupes":
        return [_proposal("Fix the build", ids[:1]), _proposal("fix  the BUILD!", ids[:1])]
    if mode == "one_bad":
        return [_proposal("Good work", ids[:1]), {**_proposal("Bad work", ids[:1]), "owner": "someone"}]
    if mode == "echo_rejected":
        rejected = [i for i in all_ids if i.startswith("rejected-")]
        return [_proposal("Cites a rejected example", rejected[:1] or ["rejected-none"])]
    if mode == "held_guess":
        return [_proposal("Guess at a held item", ["t-term"])]
    if mode == "canary":
        return [_proposal(f"Leak {os.environ.get('FAKE_CLAUDE_TERM', 'x')} now", ids[:1])]
    return [_proposal("Reply to the open thread", ids[:1]),
            _proposal("Plan the follow up", ids[-1:], kind="followup", due_hint="2026-10-09")]


def _envelope(stdin_text: str, **over: object) -> dict:
    env = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if "task proposals" in _system_prompt():
        result = _proposals_result(stdin_text)
        env["result"] = result if isinstance(result, str) else json.dumps(result)
        env.update(over)
        return env
    if "memory candidates" in _system_prompt():
        env["result"] = json.dumps(_candidates_result(stdin_text))
        env.update(over)
        return env
    ids = _ids(stdin_text)
    result = {
        "headline": "Quiet night, one open thread needs a reply",
        "attention": [{"id": ids[0], "why": "Synthetic reason"}] if ids else [],
        "summaries": {i: f"Synthetic summary for {i}" for i in ids},
        "notes": "",
    }
    env["result"] = json.dumps(result)
    env.update(over)
    return env


def _ask_envelope(stdin_text: str, extra_ids: tuple[str, ...] = ()) -> dict:
    ids = _ids(stdin_text)
    body = {"answer": "Synthetic answer: two threads are open.", "ids": [*ids[:2], *extra_ids]}
    return _envelope(stdin_text, result=json.dumps(body))


def _stream(events: list[dict]) -> int:
    """stream-json: one JSON event per line, the result event last."""
    sys.stdout.write("\n".join(json.dumps(e) for e in events) + "\n")
    sys.stdout.flush()
    return 0


def _clickup_events(scenario: str) -> list[dict]:
    result = json.loads(CLICKUP_FIXTURE.read_text(encoding="utf-8"))
    tasks = json.loads(result["result"])
    tool = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": CLICKUP_TOOL, "input": {"assignees": ["me"]}}]}}
    if scenario == "clickup_none":
        result["result"] = "[]"
    elif scenario == "clickup_empty":
        result["result"] = ""
    elif scenario == "clickup_malformed":
        result["result"] = "Here are your tasks: there are none today."
    elif scenario == "clickup_badshape":
        result["result"] = json.dumps({"tasks": tasks})
    elif scenario == "clickup_over30":
        result["result"] = json.dumps([{**tasks[0], "task_id": f"86x{n:05d}"} for n in range(35)])
    elif scenario == "clickup_one_bad":
        result["result"] = json.dumps([tasks[0], {**tasks[1], "task_id": "not a valid id!"}, tasks[2]])
    elif scenario == "clickup_forbidden_tool":
        tool["message"]["content"][0]["name"] = "Bash"
    elif scenario == "clickup_toolsearch":
        tool["message"]["content"][0]["name"] = "ToolSearch"
    elif scenario == "clickup_denial":
        result["permission_denials"] = [{"tool_name": "Read", "tool_use_id": "toolu_2", "tool_input": {}}]
    return [
        {"type": "system", "subtype": "init", "session_id": result["session_id"],
         "tools": [CLICKUP_TOOL], "mcp_servers": [{"name": "claude.ai ClickUp", "status": "connected"}]},
        tool,
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "[]"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": result["result"]}]}},
        result,
    ]


def _emit(env: dict, code: int = 0) -> int:
    sys.stdout.write(json.dumps(env))
    sys.stdout.flush()
    return code


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--version"]:
        _record("")
        print("2.1.289 (Claude Code, synthetic)")
        return 0
    if args[:1] == ["--help"]:
        _record("")
        sys.stdout.write(_help())
        return 0

    stdin_text = sys.stdin.buffer.read().decode("utf-8")
    _record(stdin_text)
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "ok")
    if "stream-json" in args and scenario == "ok":
        # The clickup_read profile asks for stream-json; under the plain "ok" scenario it gets the
        # success stream unless the test names a variant, so one runner can serve both calls.
        scenario = os.environ.get("FAKE_CLAUDE_CLICKUP", "clickup_ok")

    if scenario == "ok":
        return _emit(_envelope(stdin_text))
    if scenario == "ask_ok":
        return _emit(_ask_envelope(stdin_text))
    if scenario == "ask_hallucinated":
        return _emit(_ask_envelope(stdin_text, ("invented-9",)))
    if scenario == "ask_bad_schema":
        return _emit(_envelope(stdin_text, result=json.dumps({"ids": []})))  # no answer
    if scenario.startswith("clickup_"):
        return _stream(_clickup_events(scenario))
    if scenario == "fenced":
        env = _envelope(stdin_text)
        env["result"] = "```json\n" + env["result"] + "\n```"
        return _emit(env)
    if scenario == "hallucinated":
        env = _envelope(stdin_text)
        body = json.loads(env["result"])
        body["attention"].append({"id": "invented-1", "why": "Not in the data"})
        body["summaries"]["invented-2"] = "Not in the data"
        env["result"] = json.dumps(body)
        return _emit(env)
    if scenario in ("timeout", "hang"):
        time.sleep(3600 if scenario == "hang" else 60)
        return _emit(_envelope(stdin_text))
    if scenario == "429":
        return _emit(_envelope(stdin_text, is_error=True, subtype="success", result="API Error: Rate limit reached",
                               api_error_status=429, total_cost_usd=0.0,
                               usage={"input_tokens": 0, "output_tokens": 0}), 1)
    if scenario == "401":
        return _emit(_envelope(stdin_text, is_error=True, result="Invalid API key. Please run /login",
                               api_error_status=401, total_cost_usd=0.0,
                               usage={"input_tokens": 0, "output_tokens": 0}), 1)
    if scenario == "500":
        return _emit(_envelope(stdin_text, is_error=True, result="API Error: Internal server error",
                               api_error_status=500, total_cost_usd=0.0,
                               usage={"input_tokens": 0, "output_tokens": 0}), 1)
    if scenario == "invalid_json":
        sys.stdout.write("this is not json {")
        return 0
    if scenario == "bad_schema":
        env = _envelope(stdin_text)
        env["result"] = json.dumps({"attention": [], "summaries": {}})  # no headline
        return _emit(env)
    if scenario == "isolation_violation":
        env = _envelope(stdin_text)
        env["usage"] = {"input_tokens": 300, "output_tokens": 150,
                        "cache_creation_input_tokens": 20000, "cache_read_input_tokens": 0}
        return _emit(env)
    if scenario == "breach":
        target = Path(os.environ["FAKE_CLAUDE_TOUCH"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
        age = float(os.environ.get("FAKE_CLAUDE_TOUCH_AGE", "0"))
        if age:
            past = time.time() - age
            os.utime(target, (past, past))
        return _emit(_envelope(stdin_text))
    if scenario == "no_cost":
        env = _envelope(stdin_text)
        del env["total_cost_usd"]
        return _emit(env)
    if scenario == "inf_cost":
        sys.stdout.write(json.dumps(_envelope(stdin_text, total_cost_usd=float("inf"))))
        return 0
    sys.stderr.write(f"unknown scenario {scenario}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
