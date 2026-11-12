import json
import os
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


import subprocess


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _origin(tmp_path):
    """A bare repo with one commit on main, standing in for GitHub."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    (seed / "README").write_text("hi\n")
    _git("add", ".", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init", cwd=seed)
    _git("clone", "--bare", str(seed), str(tmp_path / "origin.git"), cwd=tmp_path)
    return str(tmp_path / "origin.git")


def test_workspace_and_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "WORK", tmp_path / "work")
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "agent")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "agent@life-os")
    # fake gh: `pr view` finds nothing; `pr create` prints a PR url
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "gh").write_text('#!/bin/sh\n[ "$2" = view ] && exit 1\necho https://github.com/me/r/pull/7\n')
    (bin_ / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")

    t = {"id": 5, "kind": "code", "channel_id": 1, "repo_url": _origin(tmp_path),
         "default_branch": "main", "title": "Add notes"}
    wt = agents.workspace(t)
    assert _git("branch", "--show-current", cwd=wt) == "agent/5"
    assert agents.publish(t, wt) is None                     # nothing changed: no PR
    (wt / "NOTES").write_text("x\n")                         # the agent left this uncommitted
    assert agents.publish(t, wt) == "https://github.com/me/r/pull/7"
    assert _git("log", "-1", "--format=%s", "agent/5", cwd=tmp_path / "origin.git") == "Add notes"

    # a later turn after the worktree was pruned continues from the pushed branch
    import shutil
    shutil.rmtree(wt)
    _git("worktree", "prune", cwd=agents.WORK / "repos" / "1")
    assert (agents.workspace(t) / "NOTES").exists()


def test_research_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "WORK", tmp_path / "work")
    wt = agents.workspace({"id": 9, "kind": "research", "repo_url": None})
    assert wt.is_dir() and wt == tmp_path / "work" / "scratch" / "9"
