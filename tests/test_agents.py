import json
import stat
import textwrap
import time

from app import agents

CODER = {"id": 1, "name": "coder"}
WRITER = {"id": 2, "name": "writer"}


def test_pick_agent():
    assert agents.pick_agent([CODER, WRITER], "@writer draft the post") == (WRITER, "draft the post")
    assert agents.pick_agent([CODER, WRITER], "fix the bug") == (CODER, "fix the bug")
    # an unknown @name is just text, for the first agent
    assert agents.pick_agent([CODER, WRITER], "@nobody hi") == (CODER, "@nobody hi")
    assert agents.pick_agent([], "hi") == (None, "hi")


def test_build_cmd():
    cmd = agents.build_cmd({"kind": "research", "model": "opus", "role": "Find sources."}, None)
    assert cmd[:2] == ["claude", "-p"] and "--resume" not in cmd
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch,Read"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,WebFetch,Read"
    assert cmd[cmd.index("--model") + 1] == "opus"
    code = agents.build_cmd({"kind": "code", "model": "sonnet", "role": "Coder."}, "abc")
    assert code[code.index("--resume") + 1] == "abc"
    assert "Commit" in code[code.index("--append-system-prompt") + 1]


def test_parse_event():
    assert agents.parse_event('{"type":"system","subtype":"init","session_id":"s1"}')["session"] == "s1"
    ev = agents.parse_event(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": " Looking. "},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "app/main.py", "old_string": "x"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q\necho done"}},
    ]}}))
    assert ev["messages"] == [("agent", "Looking."), ("system", "Edit app/main.py"), ("system", "Bash pytest -q")]
    assert agents.parse_event('{"type":"result","subtype":"success","is_error":false,"result":"hi"}')["result"] == (True, "hi")
    assert agents.parse_event('{"type":"result","subtype":"success","is_error":true,"result":"limit"}')["result"] == (False, "limit")
    assert agents.parse_event("not json") == {"session": None, "messages": [], "result": None}


def _script(tmp_path, body):
    p = tmp_path / "fake-claude"
    p.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return [str(p)]


def test_run_claude_streams_lines(tmp_path):
    cmd = _script(tmp_path, """\
        read prompt
        echo '{"type":"system","subtype":"init","session_id":"s1"}'
        echo "got: $prompt"
        echo oops >&2
        """)
    lines = []
    assert agents.run_claude(cmd, tmp_path, "hello\n", lines.append, lambda: False) == "ok"
    assert lines[1].strip() == "got: hello" and "oops\n" in lines


def test_run_claude_stop_and_timeout(tmp_path):
    cmd = _script(tmp_path, "sleep 30\n")
    t0 = time.monotonic()
    assert agents.run_claude(cmd, tmp_path, "", lambda _: None, lambda: True) == "stopped"
    assert agents.run_claude(cmd, tmp_path, "", lambda _: None, lambda: False, timeout=1) == "timeout"
    assert time.monotonic() - t0 < 10


def test_run_claude_exit_code(tmp_path):
    cmd = _script(tmp_path, "echo bad; exit 3\n")
    assert agents.run_claude(cmd, tmp_path, "", lambda _: None, lambda: False).startswith("exit 3")
