"""Shared capture logic for the web app and the Telegram bot."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import config, db

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6,
}
RELATIVE = {"today": 0, "오늘": 0, "tomorrow": 1, "tmr": 1, "내일": 1, "모레": 2}


@dataclass
class QuickTodo:
    title: str
    due_on: date | None = None
    priority: int = 2
    estimate_min: int | None = None
    project: str | None = None


def today() -> date:
    return datetime.now(config.TZ).date()


def _parse_due(token: str, base: date) -> date | None:
    t = token.lower()
    if t in RELATIVE:
        return base + timedelta(days=RELATIVE[t])
    if t in WEEKDAYS:
        # Next occurrence; today's weekday means a week from today.
        return base + timedelta(days=(WEEKDAYS[t] - base.weekday() - 1) % 7 + 1)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})", t):
        d = date(base.year, int(m[1]), int(m[2]))
        return d if d >= base else d.replace(year=base.year + 1)
    return None


def parse_quick_todo(text: str, base: date | None = None) -> QuickTodo:
    """Parse trailing modifiers: `buy gift for mom fri !1 30m #family`.

    Only tokens at the end are read as modifiers, so words inside the title stay untouched.
    """
    base = base or today()
    words = text.split()
    todo = QuickTodo(title="")
    while len(words) > 1:
        tok = words[-1]
        if m := re.fullmatch(r"!([1-3])", tok):
            todo.priority = int(m[1])
        elif m := re.fullmatch(r"(\d+)(m|h)", tok):
            todo.estimate_min = int(m[1]) * (60 if m[2] == "h" else 1)
        elif tok.startswith("#") and len(tok) > 1:
            todo.project = tok[1:]
        else:
            try:
                due = _parse_due(tok, base)
            except ValueError:  # e.g. 2/30
                due = None
            if due is None:
                break
            todo.due_on = due
        words.pop()
    todo.title = " ".join(words)
    return todo


def add_todo(text: str, source: str, default_due: date | None = None) -> dict:
    q = parse_quick_todo(text)
    q.due_on = q.due_on or default_due
    with db.connect() as conn:
        return conn.execute(
            "insert into todos (title, due_on, priority, estimate_min, project, source) "
            "values (%s, %s, %s, %s, %s, %s) returning *",
            (q.title, q.due_on, q.priority, q.estimate_min, q.project, source),
        ).fetchone()


def set_todo_status(todo_id: int, status: str) -> dict | None:
    with db.connect() as conn:
        return conn.execute(
            "update todos set status = %s, done_at = case when %s = 'done' then now() end "
            "where id = %s returning *",
            (status, status, todo_id),
        ).fetchone()


OPEN_TODOS_SQL = "select * from todos where status = 'open' order by due_on nulls last, priority, created_at"


def open_todos() -> list[dict]:
    with db.connect() as conn:
        return conn.execute(OPEN_TODOS_SQL).fetchall()


# --- Scratchpad: Claude sorts free text into todos -----------------------------------------------

SCRATCH_SYSTEM = """You turn a person's scratchpad note into todos for their personal task app.

- One todo per thing they need to do. Leave out lines that aren't actions (feelings, facts, ideas \
with nothing to do). If the whole note is a single thing, it is one todo.
- title: short and plain, in the language they wrote it in (Korean stays Korean). Keep names and \
specifics; don't invent details.
- due_on: YYYY-MM-DD, resolved against today's date. A weekday name means the next one after \
today. null when no day is given (or the default day, when one is given).
- priority: 1 if they say it's urgent or important, 3 for someday/maybe, else 2.
- estimate_min: only when they state how long it takes.
- project: one of their existing projects when it clearly fits, a new short lowercase word only \
when they name one, else null.
- notes: details from the note worth keeping with the todo (a number, an address, a link), else null.
- They may use shorthand: !1 = priority 1, 30m / 2h = estimate, #word = project, tmr = tomorrow.
- question: if something important is ambiguous (which day, whether it's a task at all), one short \
question in their language; else null. Still add your best guess."""

SCRATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due_on": {"type": ["string", "null"]},
                "priority": {"type": "integer", "enum": [1, 2, 3]},
                "estimate_min": {"type": ["integer", "null"]},
                "project": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
            },
            "required": ["title", "due_on", "priority", "estimate_min", "project", "notes"],
        }},
        "question": {"type": ["string", "null"]},
    },
    "required": ["todos", "question"],
}


def scratch(text: str, source: str, default_due: date | None = None) -> int:
    """Save scratchpad text for sorting; returns the capture id. Run `process` on it next."""
    with db.connect() as conn:
        return conn.execute("insert into captures (body, source, default_due) values (%s, %s, %s) returning id",
                            (text.strip(), source, default_due)).fetchone()["id"]


def _ask_claude(c: dict) -> tuple[list[dict], str | None]:
    from . import claude

    base = today()
    with db.connect() as conn:
        projects = [r["project"] for r in conn.execute(
            "select distinct project from todos where project is not null and status = 'open'")]
    prompt = (f"Today is {base.isoformat()} ({base.strftime('%A')}).\n"
              + (f"Default day: {c['default_due'].isoformat()} (written on that day's page).\n" if c["default_due"] else "")
              + f"Existing projects: {', '.join(projects) or 'none'}.\n\nScratchpad:\n{c['body']}")
    out = claude.ask(prompt, SCRATCH_SYSTEM, SCRATCH_SCHEMA)
    todos = []
    for t in out["todos"]:
        try:
            due = date.fromisoformat(t["due_on"]) if t.get("due_on") else c["default_due"]
        except ValueError:
            due = c["default_due"]
        if t.get("title", "").strip():
            todos.append({"title": t["title"].strip(), "due_on": due, "priority": t.get("priority") or 2,
                          "estimate_min": t.get("estimate_min"), "project": t.get("project"),
                          "notes": t.get("notes")})
    return todos, out.get("question")


def _fallback(c: dict) -> list[dict]:
    """Without Claude: one todo per line, with the old trailing-modifier shorthand."""
    out = []
    for line in c["body"].splitlines():
        if line := line.strip().lstrip("-*•").strip():
            q = parse_quick_todo(line)
            out.append({"title": q.title, "due_on": q.due_on or c["default_due"], "priority": q.priority,
                        "estimate_min": q.estimate_min, "project": q.project, "notes": None})
    return out


def process(capture_id: int) -> None:
    """Sort a pending capture into todos. Safe to run twice: only the first run adds anything."""
    with db.connect() as conn:
        c = conn.execute("select * from captures where id = %s and status = 'pending'", (capture_id,)).fetchone()
    if not c:
        return
    try:
        todos, question, status = *_ask_claude(c), "done"
    except Exception as e:
        print(f"capture {capture_id}: Claude failed, adding line by line: {e}", flush=True)
        todos, question, status = _fallback(c), None, "fallback"
    with db.connect() as conn, conn.transaction():
        if not conn.execute("update captures set status = %s, note = %s, done_at = now() "
                            "where id = %s and status = 'pending' returning id",
                            (status, question, capture_id)).fetchone():
            return  # another run got there first
        for t in todos:
            conn.execute(
                "insert into todos (title, due_on, priority, estimate_min, project, notes, source, capture_id) "
                "values (%(title)s, %(due_on)s, %(priority)s, %(estimate_min)s, %(project)s, %(notes)s, %(source)s, %(cid)s)",
                {**t, "source": c["source"], "cid": capture_id})


def process_stale(older_than_s: int = 120) -> int:
    """Pick up captures whose web process died before sorting them."""
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "select id from captures where status = 'pending' and created_at < now() - make_interval(secs => %s) "
            "order by id", (older_than_s,))]
    for i in ids:
        process(i)
    return len(ids)


def load_capture(capture_id: int) -> dict | None:
    with db.connect() as conn:
        c = conn.execute("select * from captures where id = %s", (capture_id,)).fetchone()
        if c:
            c["todos"] = conn.execute("select * from todos where capture_id = %s order by id", (capture_id,)).fetchall()
    return c


def undo_capture(capture_id: int) -> None:
    with db.connect() as conn, conn.transaction():
        conn.execute("delete from todos where capture_id = %s", (capture_id,))
        conn.execute("update captures set status = 'undone' where id = %s and status in ('done', 'fallback')",
                     (capture_id,))
