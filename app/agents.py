"""Agents tab: project channels with Claude Code agents that take tasks.

The web app uses the data functions at the top. The runner (`python -m app.agents`, bottom half)
takes one queued task at a time, runs `claude -p` in the task's workspace and streams its messages
into the thread. For code agents it then pushes branch agent/<id> and opens a PR.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque

from . import db


def pick_agent(agents: list[dict], text: str) -> tuple[dict | None, str]:
    """`@name rest` goes to that agent; anything else to the channel's first agent."""
    if m := re.match(r"@(\S+)\s+", text):
        for a in agents:
            if a["name"] == m[1]:
                return a, text[m.end():]
    return (agents[0] if agents else None), text


def channels() -> list[dict]:
    with db.connect() as conn:
        return conn.execute(
            """select c.*, exists (select 1 from agent_tasks t join agents a on a.id = t.agent_id
                                  where a.channel_id = c.id
                                    and t.finished_at > coalesce(t.seen_at, '-infinity')) as unread
               from agent_channels c order by c.name""").fetchall()


def channel(cid: int) -> dict | None:
    with db.connect() as conn:
        return conn.execute("select * from agent_channels where id = %s", (cid,)).fetchone()


def channel_agents(cid: int) -> list[dict]:
    with db.connect() as conn:
        return conn.execute("select * from agents where channel_id = %s order by id", (cid,)).fetchall()


def save_channel(name: str, repo_url: str, default_branch: str, cid: int | None = None) -> int:
    args = (name.strip(), repo_url.strip() or None, default_branch.strip() or "main")
    with db.connect() as conn:
        if cid is None:
            return conn.execute("insert into agent_channels (name, repo_url, default_branch) "
                                "values (%s, %s, %s) returning id", args).fetchone()["id"]
        conn.execute("update agent_channels set name = %s, repo_url = %s, default_branch = %s "
                     "where id = %s", (*args, cid))
        return cid


def save_agent(channel_id: int, name: str, kind: str, model: str, role: str,
               aid: int | None = None) -> int:
    args = (name.strip().lstrip("@"), kind, model.strip() or "sonnet", role.strip())
    with db.connect() as conn:
        if aid is None:
            return conn.execute("insert into agents (channel_id, name, kind, model, role) "
                                "values (%s, %s, %s, %s, %s) returning id",
                                (channel_id, *args)).fetchone()["id"]
        conn.execute("update agents set name = %s, kind = %s, model = %s, role = %s "
                     "where id = %s and channel_id = %s", (*args, aid, channel_id))
        return aid


def channel_tasks(cid: int, limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """select t.*, a.name as agent_name from agent_tasks t join agents a on a.id = t.agent_id
               where a.channel_id = %s order by t.id desc limit %s""", (cid, limit)).fetchall()
    return rows[::-1]


def task(tid: int) -> dict | None:
    with db.connect() as conn:
        return conn.execute(
            """select t.*, a.name as agent_name, a.kind, a.model, a.role, a.channel_id,
                      c.name as channel_name, c.repo_url, c.default_branch
               from agent_tasks t join agents a on a.id = t.agent_id
               join agent_channels c on c.id = a.channel_id where t.id = %s""", (tid,)).fetchone()


def messages(tid: int) -> list[dict]:
    with db.connect() as conn:
        return conn.execute("select * from agent_messages where task_id = %s order by id",
                            (tid,)).fetchall()


def start_task(agent_id: int, text: str) -> int:
    title = text.strip().splitlines()[0][:120]
    with db.connect() as conn, conn.transaction():
        tid = conn.execute("insert into agent_tasks (agent_id, title) values (%s, %s) returning id",
                           (agent_id, title)).fetchone()["id"]
        conn.execute("insert into agent_messages (task_id, author, body) values (%s, 'me', %s)",
                     (tid, text.strip()))
    return tid


def reply(tid: int, text: str) -> bool:
    """A follow-up in the thread: queued as the next turn of the same session. Not while running."""
    with db.connect() as conn, conn.transaction():
        row = conn.execute("select status from agent_tasks where id = %s for update", (tid,)).fetchone()
        if not row or row["status"] == "running":
            return False
        conn.execute("insert into agent_messages (task_id, author, body) values (%s, 'me', %s)",
                     (tid, text.strip()))
        conn.execute("update agent_tasks set status = 'queued', queued_at = now(), error = null, "
                     "finished_at = null where id = %s and status <> 'queued'", (tid,))
    return True


def cancel(tid: int) -> None:
    with db.connect() as conn:
        conn.execute("update agent_tasks set status = 'cancelled', finished_at = now() "
                     "where id = %s and status in ('queued', 'running')", (tid,))


def mark_seen(tid: int) -> None:
    with db.connect() as conn:
        conn.execute("update agent_tasks set seen_at = now() where id = %s", (tid,))


# --- runner -------------------------------------------------------------------------------------

TOOLS = {"code": ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
         "research": ["WebSearch", "WebFetch", "Read"]}
TIMEOUT = int(os.environ.get("AGENT_TIMEOUT_MIN", "30")) * 60
CODE_RULES = ("You work in a git worktree on your own branch. Commit your work with clear messages "
              "when you're done. Don't push or open a pull request: that's done for you afterwards.")


def build_cmd(agent: dict, session_id: str | None) -> list[str]:
    tools = ",".join(TOOLS[agent["kind"]])
    role = agent["role"] + ("\n\n" + CODE_RULES if agent["kind"] == "code" else "")
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", agent["model"],
           "--append-system-prompt", role.strip(), "--tools", tools, "--allowedTools", tools,
           "--permission-mode", "acceptEdits"]
    return cmd + ["--resume", session_id] if session_id else cmd


def _tool_line(name: str, inp: dict) -> str:
    arg = next((str(inp[k]) for k in ("file_path", "command", "pattern", "query", "url") if inp.get(k)), "")
    return f"{name} {arg.splitlines()[0][:100] if arg else ''}".strip()


def parse_event(line: str) -> dict:
    """One line of `claude -p --output-format stream-json`: the session id, thread messages, result."""
    out = {"session": None, "messages": [], "result": None}
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return out
    if ev.get("type") == "system" and ev.get("subtype") == "init":
        out["session"] = ev.get("session_id")
    elif ev.get("type") == "assistant":
        for b in (ev.get("message") or {}).get("content") or []:
            if b.get("type") == "text" and b.get("text", "").strip():
                out["messages"].append(("agent", b["text"].strip()))
            elif b.get("type") == "tool_use":
                out["messages"].append(("system", _tool_line(b.get("name", "?"), b.get("input") or {})))
    elif ev.get("type") == "result":
        ok = ev.get("subtype") == "success" and not ev.get("is_error")
        out["result"] = (ok, str(ev.get("result") or ev.get("subtype") or ""))
    return out


def run_claude(cmd, cwd, prompt: str, on_line, should_stop, timeout: int = TIMEOUT) -> str:
    """Run `cmd` in `cwd` with `prompt` on stdin, handing each output line to `on_line`.

    Returns "ok", "stopped" (should_stop() said so), "timeout", or "exit N: <last output>".
    """
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    proc.stdin.write(prompt)
    proc.stdin.close()
    why: list[str] = []

    def watch():
        end = time.monotonic() + timeout
        while proc.poll() is None:
            if should_stop():
                why.append("stopped")
            elif time.monotonic() > end:
                why.append("timeout")
            if why:
                os.killpg(proc.pid, signal.SIGKILL)  # the whole group: claude's own children too
                return
            time.sleep(1)

    threading.Thread(target=watch, daemon=True).start()
    tail = deque(maxlen=10)
    for line in proc.stdout:
        tail.append(line)
        on_line(line)
    proc.wait()
    if why:
        return why[0]
    return "ok" if proc.returncode == 0 else f"exit {proc.returncode}: {''.join(tail)[-500:]}"
