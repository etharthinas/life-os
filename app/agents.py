"""Agents tab: project channels with Claude Code agents that take tasks.

The web app uses the data functions at the top. The runner (`python -m app.agents`, bottom half)
takes one queued task at a time, runs `claude -p` in the task's workspace and streams its messages
into the thread. For code agents it then pushes branch agent/<id> and opens a PR.
"""

import html
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import traceback
from collections import deque
from pathlib import Path

from . import config, db


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


WORK = Path(os.environ.get("AGENTS_WORK_DIR", "/work"))


def _git(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def workspace(t: dict) -> Path:
    """Where the task's turns run. Code channels: a worktree of the repo on branch agent/<id>, kept
    across turns so `--resume` finds the same project. Otherwise an empty scratch folder."""
    if t["kind"] != "code" or not t["repo_url"]:
        d = WORK / "scratch" / str(t["id"])
        d.mkdir(parents=True, exist_ok=True)
        return d
    repo, wt, branch = WORK / "repos" / str(t["channel_id"]), WORK / "worktrees" / str(t["id"]), f"agent/{t['id']}"
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", t["repo_url"], str(repo), cwd=repo.parent)
    if not wt.exists():
        _git("fetch", "origin", cwd=repo)
        pushed = subprocess.run(["git", "rev-parse", "--verify", "-q", f"origin/{branch}"], cwd=repo,
                                capture_output=True).returncode == 0
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "-B", branch, str(wt), f"origin/{branch if pushed else t['default_branch']}", cwd=repo)
    return wt


def publish(t: dict, wt: Path) -> str | None:
    """Commit what the agent left, push agent/<id>, and return its PR (opened if new). None: no changes."""
    _git("add", "-A", cwd=wt)
    if _git("status", "--porcelain", cwd=wt):
        _git("commit", "-m", t["title"], cwd=wt)
    if not _git("rev-list", f"origin/{t['default_branch']}..HEAD", cwd=wt):
        return None
    branch = f"agent/{t['id']}"
    _git("push", "-f", "origin", f"HEAD:refs/heads/{branch}", cwd=wt)
    seen = subprocess.run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"], cwd=wt,
                          capture_output=True, text=True)
    if seen.returncode == 0 and seen.stdout.strip():
        return seen.stdout.strip()
    out = subprocess.run(["gh", "pr", "create", "--head", branch, "--base", t["default_branch"],
                          "--title", t["title"], "--body", f"Asked for in life-os: {config.PUBLIC_URL}/agents/t/{t['id']}"],
                         cwd=wt, check=True, capture_output=True, text=True).stdout
    m = re.search(r"https://\S+/pull/\d+", out)
    return m[0] if m else out.strip()


def _add_message(tid: int, author: str, body: str) -> None:
    with db.connect() as conn:
        conn.execute("insert into agent_messages (task_id, author, body) values (%s, %s, %s)", (tid, author, body))


def _set(tid: int, column: str, value) -> None:
    assert column in ("session_id", "pr_url")
    with db.connect() as conn:
        conn.execute(f"update agent_tasks set {column} = %s where id = %s", (value, tid))


def _status(tid: int) -> str:
    with db.connect() as conn:
        return conn.execute("select status from agent_tasks where id = %s", (tid,)).fetchone()["status"]


def _finish(tid: int, status: str, error: str | None = None) -> None:
    with db.connect() as conn:  # a cancel that came in meanwhile stays a cancel
        conn.execute("update agent_tasks set status = %s, error = %s, finished_at = now() "
                     "where id = %s and status = 'running'", (status, error, tid))


def _prompt(tid: int) -> str:
    """What I've said since the agent last spoke: the request, or the follow-ups."""
    with db.connect() as conn:
        rows = conn.execute(
            """select body from agent_messages where task_id = %s and author = 'me' and id >
                 coalesce((select max(id) from agent_messages where task_id = %s and author <> 'me'), 0)
               order by id""", (tid, tid)).fetchall()
    return "\n\n".join(r["body"] for r in rows)


def _claim() -> int | None:
    with db.connect() as conn:
        row = conn.execute(
            """update agent_tasks set status = 'running', started_at = now()
               where id = (select id from agent_tasks where status = 'queued'
                           order by queued_at, id for update skip locked limit 1)
               returning id""").fetchone()
    return row["id"] if row else None


def notify(t: dict) -> None:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    from .telegram import Bot
    e = html.escape
    lines = [f"<b>{e(t['channel_name'])}/{e(t['agent_name'])}</b> {t['status']}: {e(t['title'])}"]
    if t["pr_url"]:
        lines.append(e(t["pr_url"]))
    if t["status"] == "failed" and t["error"]:
        lines.append(e(t["error"][:300]))
    lines.append(f"{config.PUBLIC_URL}/agents/t/{t['id']}")
    try:
        Bot(config.TELEGRAM_BOT_TOKEN).send(int(config.TELEGRAM_CHAT_ID), "\n".join(lines))
    except Exception:
        traceback.print_exc()


def run_turn(tid: int) -> None:
    t = task(tid)
    try:
        cwd = workspace(t)
    except subprocess.CalledProcessError as e:
        _add_message(tid, "system", f"Couldn't get the repo ready: {(e.stderr or '').strip()[-500:]}")
        _finish(tid, "failed", "workspace")
        notify(task(tid))
        return
    result: list[tuple[bool, str]] = []

    def on_line(line: str) -> None:
        ev = parse_event(line)
        if ev["session"] and ev["session"] != t["session_id"]:
            t["session_id"] = ev["session"]
            _set(tid, "session_id", ev["session"])
        for author, body in ev["messages"]:
            _add_message(tid, author, body)
        if ev["result"]:
            result.append(ev["result"])

    outcome = run_claude(build_cmd(t, t["session_id"]), cwd, _prompt(tid), on_line,
                         lambda: _status(tid) == "cancelled")
    if outcome == "stopped":
        _add_message(tid, "system", "Cancelled.")
        return
    ok, text = result[-1] if result else (False, outcome)
    if outcome == "timeout":
        ok, text = False, f"Stopped after {TIMEOUT // 60} minutes."
    if ok and t["kind"] == "code" and t["repo_url"]:
        try:
            if url := publish(t, cwd):
                _set(tid, "pr_url", url)
                _add_message(tid, "system", f"PR: {url}")
        except subprocess.CalledProcessError as e:
            ok, text = False, f"Couldn't push or open the PR: {(e.stderr or '').strip()[-500:]}"
    if not ok:
        _add_message(tid, "system", text[:1000] or "Failed.")
    _finish(tid, "done" if ok else "failed", None if ok else text[:500])
    notify(task(tid))


def prune(days: int = 7) -> None:
    """Free disk: remove workspaces of tasks finished over a week ago. Their branches stay on GitHub,
    and a later reply makes a fresh worktree from the pushed branch."""
    with db.connect() as conn:
        old = [r["id"] for r in conn.execute(
            "select id from agent_tasks where status not in ('queued', 'running') "
            "and finished_at < now() - make_interval(days => %s)", (days,))]
    for tid in old:
        for d in (WORK / "worktrees" / str(tid), WORK / "scratch" / str(tid)):
            shutil.rmtree(d, ignore_errors=True)
    for repo in (WORK / "repos").glob("*"):
        subprocess.run(["git", "worktree", "prune"], cwd=repo)


def run() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    for k, v in (("GIT_AUTHOR_NAME", "life-os agent"), ("GIT_COMMITTER_NAME", "life-os agent"),
                 ("GIT_AUTHOR_EMAIL", "agent@life-os"), ("GIT_COMMITTER_EMAIL", "agent@life-os")):
        os.environ.setdefault(k, v)
    subprocess.run(["gh", "auth", "setup-git"])  # git pushes over https with GH_TOKEN
    with db.connect() as conn:
        conn.execute("update agent_tasks set status = 'failed', error = 'runner restarted', "
                     "finished_at = now() where status = 'running'")
    print("agents runner started", flush=True)
    last_prune = 0.0
    while True:
        try:
            if time.monotonic() - last_prune > 24 * 3600:
                last_prune = time.monotonic()
                prune()
            tid = _claim()
        except Exception:
            traceback.print_exc()
            time.sleep(10)
            continue
        if tid is None:
            time.sleep(3)
            continue
        print("task", tid, flush=True)
        try:
            run_turn(tid)
        except Exception as e:
            traceback.print_exc()
            try:
                _finish(tid, "failed", repr(e)[:500])
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    run()
