# Agent Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Slack-like `/agents` tab in life-os. Each project is a channel, and channels hold Claude
Code agents. A request in a channel becomes a task thread. A runner container works through the
tasks one at a time, then for code agents pushes a branch and opens a PR. Telegram pings when a
task is done.

**Architecture:**
- Four new Postgres tables (`agent_channels`, `agents`, `agent_tasks`, `agent_messages`).
- `app/agents.py` holds the data functions (used by the web app) and the runner
  (`python -m app.agents`, its own docker-compose service with a locked-down env).
- The runner claims one queued task, prepares a git worktree on branch `agent/<id>`, and runs
  `claude -p --output-format stream-json`. Each streamed event becomes a thread message.
- When a turn ends, the runner commits leftovers, pushes, opens or reuses a PR with `gh`, and
  sends a Telegram message.
- The web UI is plain Jinja + HTMX pages (channels, then channel, then thread). A running thread
  polls every 2 s.

**Tech Stack:** Python 3.14, FastAPI, Jinja2, HTMX 2, psycopg 3, Postgres (Supabase), Claude Code
CLI, git, GitHub CLI (`gh`), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-12-agent-manager-design.md`

## Global Constraints

- Plain SQL, no ORM. Use `db.connect()` (autocommit pooled connection) and
  `conn.transaction()` for multi-statement writes.
- One task runs at a time; no parallel runner.
- Code-agent tools: `Read,Edit,Write,Glob,Grep,Bash`. Research-agent tools:
  `WebSearch,WebFetch,Read`.
- Branch per task: `agent/<task-id>`. Per-turn timeout: `AGENT_TIMEOUT_MIN` (default 30).
- Agents container env holds only: `CLAUDE_CODE_OAUTH_TOKEN`, `GH_TOKEN`, `DATABASE_URL`
  (restricted `agents_runner` role), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PUBLIC_URL`.
  There is no Docker socket. The process runs as non-root user `agent`.
- The runner never calls `db.migrate()`; the web app runs migrations.
- Tests are plain pytest, no DB (the existing suite has none). Run:
  `.venv/bin/python -m pytest -q`.
- Commit messages: plain sentence style, as in `git log`. End each with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- UI copy in English, as in the rest of the app. Reuse the existing CSS tokens and classes
  (`.card`, `.btn`, `.btn-primary`, `.composer`, `.section`, `.page-head`).

Changes from the spec (simpler, same behaviour):
- No `default_agent_id` column. A request with no `@name` goes to the channel's first agent
  (lowest id).
- One page per level (channels, then channel, then thread) on every screen size, not three
  columns.
- There is no concurrent-claim test, since only one runner exists. The claim still uses
  `FOR UPDATE SKIP LOCKED`.

---

### Task 1: Tables and data functions

**Files:**
- Create: `migrations/023_agents.sql`
- Create: `app/agents.py` (data section only in this task)
- Test: `tests/test_agents.py`

**Interfaces:**
- Produces:
  - `pick_agent(agents: list[dict], text: str) -> tuple[dict | None, str]`
  - `channels() -> list[dict]`, where each row has `unread: bool`
  - `channel(cid: int) -> dict | None`
  - `channel_agents(cid: int) -> list[dict]`
  - `save_channel(name, repo_url, default_branch, cid=None) -> int`
  - `save_agent(channel_id, name, kind, model, role, aid=None) -> int`
  - `channel_tasks(cid: int) -> list[dict]` (oldest first; each row has `agent_name`)
  - `task(tid: int) -> dict | None`, with task columns plus `agent_name, kind, model, role,
    channel_id, channel_name, repo_url, default_branch`
  - `messages(tid: int) -> list[dict]`
  - `start_task(agent_id: int, text: str) -> int`
  - `reply(tid: int, text: str) -> bool` (False while running)
  - `cancel(tid: int) -> None`
  - `mark_seen(tid: int) -> None`

- [ ] **Step 1: Write the migration**

`migrations/023_agents.sql`:

```sql
-- Agents tab: project channels, Claude Code agents in them, task threads.
create table agent_channels (
  id bigint generated always as identity primary key,
  name text not null unique,
  repo_url text,                      -- null: no repo (research only)
  default_branch text not null default 'main',
  created_at timestamptz not null default now()
);

create table agents (
  id bigint generated always as identity primary key,
  channel_id bigint not null references agent_channels on delete cascade,
  name text not null,
  kind text not null check (kind in ('code', 'research')),
  model text not null default 'sonnet',
  role text not null default '',
  created_at timestamptz not null default now(),
  unique (channel_id, name)
);

create table agent_tasks (
  id bigint generated always as identity primary key,
  agent_id bigint not null references agents on delete cascade,
  title text not null,
  status text not null default 'queued'
    check (status in ('queued', 'running', 'done', 'failed', 'cancelled')),
  session_id text,
  pr_url text,
  error text,
  queued_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  seen_at timestamptz
);
create index agent_tasks_queue on agent_tasks (queued_at) where status = 'queued';

create table agent_messages (
  id bigint generated always as identity primary key,
  task_id bigint not null references agent_tasks on delete cascade,
  author text not null check (author in ('me', 'agent', 'system')),
  body text not null,
  created_at timestamptz not null default now()
);
create index agent_messages_task on agent_messages (task_id, id);

-- The agents container connects as a role that can only touch these tables. Run once, by hand,
-- as the owner (Supabase SQL editor), with a real password:
--   create role agents_runner login password '...';
--   grant usage on schema public to agents_runner;
--   grant select on agent_channels, agents to agents_runner;
--   grant select, insert, update on agent_tasks, agent_messages to agents_runner;
-- Supabase session pooler user name: agents_runner.<project ref>
```

- [ ] **Step 2: Write the failing test for `pick_agent`**

`tests/test_agents.py`:

```python
from app import agents

CODER = {"id": 1, "name": "coder"}
WRITER = {"id": 2, "name": "writer"}


def test_pick_agent():
    assert agents.pick_agent([CODER, WRITER], "@writer draft the post") == (WRITER, "draft the post")
    assert agents.pick_agent([CODER, WRITER], "fix the bug") == (CODER, "fix the bug")
    # an unknown @name is just text, for the first agent
    assert agents.pick_agent([CODER, WRITER], "@nobody hi") == (CODER, "@nobody hi")
    assert agents.pick_agent([], "hi") == (None, "hi")
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agents.py -q`
Expected: FAIL, `ImportError: cannot import name 'agents'`.

- [ ] **Step 4: Write `app/agents.py`, data section**

```python
"""Agents tab: project channels with Claude Code agents that take tasks.

The web app uses the data functions at the top. The runner (`python -m app.agents`, bottom half)
takes one queued task at a time, runs `claude -p` in the task's workspace and streams its messages
into the thread. For code agents it then pushes branch agent/<id> and opens a PR.
"""

import re

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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_agents.py -q`
Expected: `1 passed`.

- [ ] **Step 6: Check the migration and the data functions against the dev DB**

Run:

```sh
scripts/devdb.sh start
.venv/bin/python -m app.db
.venv/bin/python -c "
from app import agents as a
c = a.save_channel('demo', '', 'main'); g = a.save_agent(c, 'coder', 'code', 'sonnet', 'be brief')
t = a.start_task(g, 'fix it\nmore detail')
assert a.task(t)['title'] == 'fix it' and a.task(t)['status'] == 'queued'
assert a.reply(t, 'also this') and len(a.messages(t)) == 2
a.cancel(t); assert a.task(t)['status'] == 'cancelled'
assert a.channels()[0]['unread']; a.mark_seen(t); assert not a.channels()[0]['unread']
print('ok')"
```

Expected: `applied: 023_agents.sql`, then `ok`. Then clean up:

```sh
.venv/bin/python -c "from app import db; db.connect().__enter__().execute(\"delete from agent_channels where name = 'demo'\")"
```

- [ ] **Step 7: Commit**

```bash
git add migrations/023_agents.sql app/agents.py tests/test_agents.py
git commit -m "Agents: channels, agents, task threads in Postgres

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Running Claude and reading its stream

**Files:**
- Modify: `app/agents.py` (append the runner core)
- Test: `tests/test_agents.py` (append)

**Interfaces:**
- Consumes: nothing from Task 1 except the module.
- Produces:
  - `TOOLS: dict[str, list[str]]`
  - `TIMEOUT: int` (seconds)
  - `build_cmd(agent: dict, session_id: str | None) -> list[str]`, where `agent` has `kind`,
    `model` and `role`
  - `parse_event(line: str) -> dict` with keys `session: str | None`,
    `messages: list[tuple[str, str]]` (author, body) and `result: tuple[bool, str] | None`
  - `run_claude(cmd, cwd, prompt, on_line, should_stop, timeout=TIMEOUT) -> str`, which returns
    `"ok"`, `"stopped"`, `"timeout"` or `"exit N: <tail>"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agents.py`:

```python
import json
import os
import stat
import textwrap
import time


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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_agents.py -q`
Expected: 5 failures, `AttributeError: module 'app.agents' has no attribute 'build_cmd'` (and
similar).

- [ ] **Step 3: Append the runner core to `app/agents.py`**

Add `import json, os, signal, subprocess, threading, time` and `from collections import deque` to
the imports at the top. Keep them sorted, stdlib first. Then append:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_agents.py -q`
Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/agents.py tests/test_agents.py
git commit -m "Agents: run claude -p with a stream, stop on cancel or timeout

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Workspaces, PRs, the runner loop, Telegram

**Files:**
- Modify: `app/agents.py` (append)
- Modify: `app/config.py` (add `PUBLIC_URL`)
- Test: `tests/test_agents.py` (append)

**Interfaces:**
- Consumes:
  - from Task 1: `task(tid)`
  - from Task 2: `build_cmd`, `parse_event`, `run_claude`, `TIMEOUT`
- Produces:
  - `WORK: Path`
  - `workspace(t: dict) -> Path`
  - `publish(t: dict, wt: Path) -> str | None` (returns the PR url)
  - `run_turn(tid: int) -> None`
  - `run() -> None` (main loop)
  - `config.PUBLIC_URL: str`

- [ ] **Step 1: Write the failing tests (local bare repo as origin, fake `gh` on PATH)**

Append to `tests/test_agents.py`:

```python
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_agents.py -q`
Expected: 2 failures, `AttributeError: ... no attribute 'WORK'`.

- [ ] **Step 3: Add `PUBLIC_URL` to `app/config.py`**

Append after `TELEGRAM_CHAT_ID`:

```python
# Where the web app is reached from my devices (links in Telegram messages and PRs).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://lifeos.tail969156.ts.net").rstrip("/")
```

- [ ] **Step 4: Append workspace, publish, the turn and the loop to `app/agents.py`**

Add `import html, shutil, traceback` and `from pathlib import Path` to the imports, and change
`from . import db` to `from . import config, db`. Then append:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: the whole suite passes, including the 8 tests in `tests/test_agents.py`.

- [ ] **Step 6: Smoke-test one real turn locally (needs `claude` logged in on the Mac)**

```sh
.venv/bin/python -c "
from app import agents as a
c = a.save_channel('smoke', '', 'main'); g = a.save_agent(c, 'r', 'research', 'haiku', 'Answer in one line.')
t = a.start_task(g, 'What is 2+2?')
assert a._claim() == t
a.WORK = __import__('pathlib').Path('.dev/agents-work'); a.run_turn(t)
print(a.task(t)['status'], [m['body'] for m in a.messages(t)])
from app import db; db.connect().__enter__().execute(\"delete from agent_channels where name = 'smoke'\")"
```

Expected: `done ['What is 2+2?', '4']` (the wording of the answer can vary).

- [ ] **Step 7: Commit**

```bash
git add app/agents.py app/config.py tests/test_agents.py
git commit -m "Agents runner: worktree per task, push and open a PR, Telegram when done

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The Agents tab

**Files:**
- Modify: `app/main.py` (import `agents`, add routes at the end, before any `if __name__`)
- Create: `app/templates/agents.html` (channel list + new channel)
- Create: `app/templates/agent_channel.html` (settings, task feed, composer)
- Create: `app/templates/agent_thread.html` (thread page)
- Create: `app/templates/_agent_thread.html` (polled fragment)
- Create: `app/templates/_agent_send.html` (Enter-to-send script)
- Modify: `app/templates/base.html` (tab bar item)
- Modify: `app/static/style.css` (tab bar to 7 columns + agents styles)

**Interfaces:**
- Consumes: from Task 1 all data functions; `agents.pick_agent`.
- Produces: routes `GET /agents`, `POST /agents/channels`, `GET /agents/c/{cid}`,
  `POST /agents/c/{cid}`, `POST /agents/c/{cid}/agents`, `POST /agents/c/{cid}/tasks`,
  `GET /agents/t/{tid}`, `GET /agents/t/{tid}/messages`, `POST /agents/t/{tid}/reply`,
  `POST /agents/t/{tid}/cancel`.

- [ ] **Step 1: Add the routes to `app/main.py`**

Add `agents` to the `from . import ...` line (alphabetical: `from . import agents, capture, …`).
Add a Jinja filter next to the other filters:

```python
templates.env.filters["md"] = lambda text: media.render_markdown(text, {}, {}, {})
```

Append the routes:

```python
# --- Agents tab ---------------------------------------------------------------------------------

def _or_404(row):
    if row is None:
        raise HTTPException(404)
    return row


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    return _render(request, "agents.html", channels=agents.channels())


@app.post("/agents/channels")
def agents_new_channel(name: str = Form(...), repo_url: str = Form(""), default_branch: str = Form("main")):
    cid = agents.save_channel(name, repo_url, default_branch)
    return RedirectResponse(f"/agents/c/{cid}", status_code=303)


@app.get("/agents/c/{cid}", response_class=HTMLResponse)
def agents_channel(request: Request, cid: int):
    ch = _or_404(agents.channel(cid))
    return _render(request, "agent_channel.html", ch=ch, members=agents.channel_agents(cid),
                   tasks=agents.channel_tasks(cid), channels=agents.channels())


@app.post("/agents/c/{cid}")
def agents_edit_channel(cid: int, name: str = Form(...), repo_url: str = Form(""), default_branch: str = Form("main")):
    _or_404(agents.channel(cid))
    agents.save_channel(name, repo_url, default_branch, cid)
    return RedirectResponse(f"/agents/c/{cid}", status_code=303)


@app.post("/agents/c/{cid}/agents")
def agents_save_agent(cid: int, name: str = Form(...), kind: str = Form("code"), model: str = Form("sonnet"),
                      role: str = Form(""), aid: int | None = Form(None)):
    _or_404(agents.channel(cid))
    if kind not in agents.TOOLS:
        raise HTTPException(400)
    agents.save_agent(cid, name, kind, model, role, aid)
    return RedirectResponse(f"/agents/c/{cid}", status_code=303)


@app.post("/agents/c/{cid}/tasks")
def agents_new_task(cid: int, text: str = Form(...)):
    _or_404(agents.channel(cid))
    agent, text = agents.pick_agent(agents.channel_agents(cid), text.strip())
    if agent is None or not text:
        raise HTTPException(400, "Add an agent to this channel first")
    return RedirectResponse(f"/agents/t/{agents.start_task(agent['id'], text)}", status_code=303)


@app.get("/agents/t/{tid}", response_class=HTMLResponse)
def agents_thread(request: Request, tid: int):
    t = _or_404(agents.task(tid))
    agents.mark_seen(tid)
    return _render(request, "agent_thread.html", t=t, msgs=agents.messages(tid))


@app.get("/agents/t/{tid}/messages", response_class=HTMLResponse)
def agents_thread_messages(request: Request, tid: int):
    t = _or_404(agents.task(tid))
    if t["status"] not in ("queued", "running"):
        agents.mark_seen(tid)
    return _render(request, "_agent_thread.html", t=t, msgs=agents.messages(tid))


@app.post("/agents/t/{tid}/reply")
def agents_reply(tid: int, text: str = Form(...)):
    if not text.strip() or not agents.reply(tid, text):
        raise HTTPException(409, "Still running: wait for it, or cancel")
    return RedirectResponse(f"/agents/t/{tid}", status_code=303)


@app.post("/agents/t/{tid}/cancel")
def agents_cancel(tid: int):
    agents.cancel(tid)
    return RedirectResponse(f"/agents/t/{tid}", status_code=303)
```

- [ ] **Step 2: Templates**

`app/templates/agents.html`:

```html
{% extends "base.html" %}
{% block title %}Agents · life-os{% endblock %}
{% block content %}
<header class="page-head"><h1>Agents</h1></header>
<section class="section">
  <div class="section-head"><h2>Channels</h2></div>
  {% if channels %}
  <ul class="card agent-list">
    {% for c in channels %}
    <li><a href="/agents/c/{{ c.id }}"># {{ c.name }}{% if c.unread %} <span class="dot" aria-label="new"></span>{% endif %}</a>
      <span class="hint">{{ c.repo_url or "no repo" }}</span></li>
    {% endfor %}
  </ul>
  {% else %}<p class="empty">No channels yet.</p>{% endif %}
</section>
<details class="section">
  <summary class="btn">New channel</summary>
  <form class="card pad agent-form" method="post" action="/agents/channels">
    <label>Name <input name="name" required placeholder="life-os"></label>
    <label>Repo <input name="repo_url" placeholder="https://github.com/me/repo.git (empty: research only)"></label>
    <label>Base branch <input name="default_branch" value="main"></label>
    <button class="btn btn-primary">Add</button>
  </form>
</details>
{% endblock %}
```

`app/templates/agent_channel.html`:

```html
{% extends "base.html" %}
{% from "_avatar.html" import back %}
{% block title %}#{{ ch.name }} · Agents{% endblock %}
{% block content %}
{{ back("/agents", "Agents") }}
<header class="page-head"><h1># {{ ch.name }}</h1></header>
<p class="hint">{{ ch.repo_url or "no repo" }}{% if ch.repo_url %} · {{ ch.default_branch }}{% endif %}
  · {% for a in members %}@{{ a.name }} <span class="muted">({{ a.kind }})</span>{% if not loop.last %}, {% endif %}{% else %}no agents yet{% endfor %}</p>

<details class="section">
  <summary class="btn btn-quiet">Channel settings</summary>
  <form class="card pad agent-form" method="post" action="/agents/c/{{ ch.id }}">
    <label>Name <input name="name" required value="{{ ch.name }}"></label>
    <label>Repo <input name="repo_url" value="{{ ch.repo_url or '' }}"></label>
    <label>Base branch <input name="default_branch" value="{{ ch.default_branch }}"></label>
    <button class="btn">Save</button>
  </form>
  {% for a in members + [none] %}
  <form class="card pad agent-form" method="post" action="/agents/c/{{ ch.id }}/agents">
    {% if a %}<input type="hidden" name="aid" value="{{ a.id }}">{% endif %}
    <label>Agent <input name="name" required value="{{ a.name if a else '' }}" placeholder="coder"></label>
    <label>Kind <select name="kind">
      {% for k in ("code", "research") %}<option {% if a and a.kind == k %}selected{% endif %}>{{ k }}</option>{% endfor %}
    </select></label>
    <label>Model <input name="model" value="{{ a.model if a else 'sonnet' }}"></label>
    <label>Role <textarea name="role" rows="3" placeholder="What this agent is for, how it should work">{{ a.role if a else '' }}</textarea></label>
    <button class="btn">{{ "Save" if a else "Add" }}</button>
  </form>
  {% endfor %}
</details>

<section class="section">
  {% for t in tasks %}
  <a class="card pad agent-task" href="/agents/t/{{ t.id }}">
    <span class="agent-status s-{{ t.status }}">{{ t.status }}</span>
    <span class="agent-title">{{ t.title }}</span>
    <span class="hint">@{{ t.agent_name }} · {{ t.queued_at | local }}{% if t.pr_url %} · PR{% endif %}</span>
  </a>
  {% else %}<p class="empty">No tasks yet. Ask below.</p>{% endfor %}
</section>

{% if members %}
<form class="composer agent-composer" method="post" action="/agents/c/{{ ch.id }}/tasks">
  <textarea name="text" rows="2" required placeholder="@{{ members[0].name }} what should be done…"></textarea>
  <div class="actions"><span class="hint">@name picks the agent. ⌘⏎ to send.</span><button class="btn btn-primary">Send</button></div>
</form>
{% endif %}
{% include "_agent_send.html" %}
{% endblock %}
```

`app/templates/agent_thread.html`:

```html
{% extends "base.html" %}
{% from "_avatar.html" import back %}
{% block title %}{{ t.title }} · Agents{% endblock %}
{% block content %}
{{ back("/agents/c/" ~ t.channel_id, "#" ~ t.channel_name) }}
<header class="page-head"><h1 class="agent-h1">{{ t.title }}</h1></header>
{% include "_agent_thread.html" %}
<form class="composer agent-composer" method="post" action="/agents/t/{{ t.id }}/reply">
  <textarea name="text" rows="2" required placeholder="Reply to @{{ t.agent_name }}…"></textarea>
  <div class="actions"><span class="hint">Continues the same session. ⌘⏎ to send.</span><button class="btn btn-primary">Send</button></div>
</form>
{% include "_agent_send.html" %}
<script>
  // Keep the newest message in view while the thread fills in, unless I've scrolled up to read.
  if (!window.agentScroll) {
    window.agentScroll = true;
    let atBottom = true;
    addEventListener("scroll", () => { atBottom = innerHeight + scrollY > document.body.scrollHeight - 200; });
    document.addEventListener("htmx:afterSwap", (e) => {
      if (e.detail.target.id === "agent-thread" && atBottom) scrollTo(0, document.body.scrollHeight);
    });
  }
  scrollTo(0, document.body.scrollHeight);
</script>
{% endblock %}
```

`app/templates/_agent_thread.html`:

```html
{% set live = t.status in ("queued", "running") %}
<div id="agent-thread" class="section"
     {% if live %}hx-get="/agents/t/{{ t.id }}/messages" hx-trigger="every 2s" hx-swap="outerHTML"{% endif %}>
  <p class="agent-meta">
    <span class="agent-status s-{{ t.status }}">{{ t.status }}</span>
    @{{ t.agent_name }}
    {% if t.pr_url %}· <a href="{{ t.pr_url }}" target="_blank" rel="noopener">PR</a>{% endif %}
    {% if t.kind == "code" and t.repo_url %}· <code>agent/{{ t.id }}</code>{% endif %}
    {% if live %}
    <form method="post" action="/agents/t/{{ t.id }}/cancel" class="inline"><button class="btn btn-quiet">Cancel</button></form>
    {% endif %}
  </p>
  <ol class="agent-msgs">
    {% for m in msgs %}
    <li class="m-{{ m.author }}">
      {% if m.author == "system" %}<span class="hint">{{ m.body }}</span>
      {% else %}<div class="m-body">{{ m.body | md }}</div>{% endif %}
    </li>
    {% endfor %}
    {% if t.status == "queued" %}<li class="m-system"><span class="hint">Waiting for its turn…</span></li>
    {% elif t.status == "running" %}<li class="m-system"><span class="hint">Working…</span></li>{% endif %}
  </ol>
</div>
```

`app/templates/_agent_send.html` (shared ⌘⏎ / Enter-to-send, same idea as `todos.html`):

```html
<script>
  if (!window.agentSend) {
    window.agentSend = true;
    // Enter sends, Shift-Enter makes a new line (phones: the button). ⌘⏎ also sends.
    document.addEventListener("keydown", (e) => {
      const ta = e.target.closest?.(".agent-composer textarea");
      if (!ta || e.key !== "Enter" || e.shiftKey || e.isComposing) return;
      if (matchMedia("(hover: none)").matches && !(e.metaKey || e.ctrlKey)) return;
      e.preventDefault();
      ta.form.requestSubmit();
    });
  }
</script>
```


- [ ] **Step 3: Tab bar item in `base.html`**

After the Todos `<li>` and before People, add:

```html
      <li><a href="/agents" {% if path.startswith("/agents") %}aria-current="page"{% endif %}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5h16v10H9l-4 3.5v-3.5H4z"/><path d="M8.5 10.5h.01M12 10.5h.01M15.5 10.5h.01"/></svg>
        Agents</a></li>
```

- [ ] **Step 4: CSS**

In `app/static/style.css`, change `grid-template-columns: repeat(6, 1fr)` in `.tabbar ul` to
`repeat(7, 1fr)`. Append:

```css
/* Agents tab */
.agent-list { list-style: none; margin: 0; padding: 4px 16px; }
.agent-list li { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--rule); }
.agent-list li:last-child { border-bottom: none; }
.agent-list .hint { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); vertical-align: middle; }
.agent-form { display: grid; gap: 10px; margin-top: 8px; }
.agent-form label { display: grid; gap: 4px; font-size: var(--t-sm); color: var(--ink-2); }
.agent-task { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; align-items: baseline; color: var(--ink); }
.agent-task .hint { grid-column: 2; }
.agent-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-status { font-size: var(--t-xs); font-weight: 600; padding: 2px 8px; border-radius: 999px; background: var(--sunken); color: var(--ink-2); }
.agent-status.s-running { background: var(--accent-soft); color: var(--accent); }
.agent-status.s-done { color: var(--ok); }
.agent-status.s-failed { color: var(--danger); }
.agent-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: var(--t-sm); color: var(--ink-2); }
.agent-meta form.inline { margin-left: auto; }
.agent-h1 { font-size: var(--t-xl) !important; overflow-wrap: anywhere; }
.agent-msgs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.agent-msgs .m-me .m-body { background: var(--accent-soft); border-radius: var(--r-md); padding: 10px 12px; margin-left: 15%; }
.agent-msgs .m-agent .m-body { background: var(--surface); border: 1px solid var(--rule); border-radius: var(--r-md); padding: 10px 12px; overflow-wrap: anywhere; }
.agent-msgs .m-system { font-family: ui-monospace, monospace; padding-left: 4px; overflow-wrap: anywhere; }
.agent-composer { position: sticky; bottom: calc(var(--tabbar-h) + env(safe-area-inset-bottom, 0px) + 8px); margin-top: 16px; }
```

- [ ] **Step 5: Run it and click through it**

```sh
.venv/bin/uvicorn app.main:app --reload
```

In a browser at `http://localhost:8000/agents`, then again at phone width (390 px):

1. Add channel `demo` with no repo.
2. Open it. In Channel settings, add agent `r`, kind `research`, model `haiku`.
3. Send `What's the capital of Australia?`. The page lands on the thread with status `queued` and
   a "Waiting for its turn…" line.
4. In a second terminal, run `AGENTS_WORK_DIR=.dev/agents-work .venv/bin/python -m app.agents`.
   Within a few seconds the thread shows `running`, then the answer, then `done`. Polling stops
   (no more requests in the network tab).
5. Reply `And of New Zealand?`. It queues, resumes the same session, and answers.
6. Send a slow one (`Search the web for three recent papers on X`) and press Cancel. The status
   becomes `cancelled` and the runner log shows the next poll.
7. On the Agents page the channel shows an unread dot until the thread is opened.

Run `.venv/bin/python -m pytest -q`. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/main.py app/templates/agents.html app/templates/agent_channel.html app/templates/agent_thread.html \
        app/templates/_agent_thread.html app/templates/_agent_send.html app/templates/base.html app/static/style.css
git commit -m "Agents tab: channels, task threads that fill in live, replies and cancel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Container and deploy

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Create: `.env.agents.example`
- Modify: `README.md` (an Agents section)

**Interfaces:**
- Consumes: `python -m app.agents` (Task 3).

- [ ] **Step 1: Dockerfile**

Add `git gh` to the `apt-get install` line. After the Claude Code install `RUN`, add:

```dockerfile
# The agents service runs as a non-root user (Claude Code's tools run anything; keep them out of root).
# /root isn't readable by it, so the claude binary is copied somewhere it is.
RUN cp -L /root/.local/bin/claude /usr/local/bin/claude \
    && useradd -m -u 1000 agent && mkdir -p /work && chown agent:agent /work
```

- [ ] **Step 2: docker-compose.yml**

Append under `services:`:

```yaml
  agents:  # Agents tab runner. Its own env file: no life-os secrets in reach of the agents' Bash.
    build: .
    env_file: .env.agents
    environment: {AGENTS_WORK_DIR: /work}
    command: ["python", "-m", "app.agents"]
    user: agent
    volumes:
      - agents-work:/work            # repos and worktrees
      - agents-home:/home/agent      # ~/.claude (sessions for --resume), git/gh config
    restart: unless-stopped
    logging: *logging
```

And at the end of the file:

```yaml
volumes:
  agents-work:
  agents-home:
```

Check: `docker compose config -q`. Expected: no output, exit 0.

- [ ] **Step 3: `.env.agents.example`**

```sh
# Env for the agents container ONLY. Never copy .env here: agents run Bash and can read their env.
CLAUDE_CODE_OAUTH_TOKEN=          # claude setup-token
# Fine-grained PAT: only the repos agents may touch; Contents + Pull requests: read/write.
GH_TOKEN=
# Supabase session pooler, as the agents_runner role (see migrations/023_agents.sql).
DATABASE_URL=postgresql://agents_runner.<project-ref>:<password>@<pooler-host>:5432/postgres
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PUBLIC_URL=https://lifeos.tail969156.ts.net
TZ_NAME=Asia/Seoul
# AGENT_TIMEOUT_MIN=30
```

Check `.gitignore` covers `.env.agents`. If `.env` is matched only exactly, add the line
`.env.agents`.

- [ ] **Step 4: README section**

Append to `README.md`:

```markdown
## Agents tab

Project channels with Claude Code agents in them (`app/agents.py`). The `agents` container takes
one task at a time. Code agents work in a git worktree on `agent/<task id>` and get a PR opened;
research agents get an empty folder and web tools. A reply in a thread continues the same session.

Setup on the VM (once):
1. Run the role SQL in the comment at the end of `migrations/023_agents.sql` (Supabase SQL editor).
2. `cp .env.agents.example .env.agents` and fill it in. Use a fine-grained GitHub token, only for
   the repos agents may touch.
3. Swap, since the box has 1 GB: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`,
   plus `/swapfile none swap sw 0 0` in `/etc/fstab`.
4. `docker compose up -d --build`.

Logs: `docker compose logs -f agents`.
```

- [ ] **Step 5: Build the image locally**

Run: `docker compose build agents && docker compose run --rm --entrypoint sh agents -c 'whoami && claude --version && gh --version | head -1 && git --version'`
Expected: `agent`, then a Claude Code version, a `gh version` line and a `git version` line.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml .env.agents.example README.md .gitignore
git commit -m "Agents container: non-root, own env file, work and home volumes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Deploy (with the user. It needs secrets and touches production.)**

Ask the user before each of these:
1. Create the `agents_runner` role in Supabase, with a password the user chooses.
2. Create a fine-grained GitHub token.
3. Create `.env.agents` on the VM.
4. Add swap.
5. Run `rsync` and `docker compose up -d --build`, in the same way the existing deploy does.

Then check: `ssh lifeos 'cd /srv/life-os && docker compose ps && docker compose logs --tail 20 agents'`.
Expected: `agents` is up and logs `agents runner started`. Run one research task from the phone
and see the Telegram message.
