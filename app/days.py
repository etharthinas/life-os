"""Day view data, planning, and accepting blocks into Google Calendar."""

import json
from datetime import date, datetime, time, timedelta

from . import config, db, google, journal, planner


def get_settings(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("select key, value from settings")}


def get_setting(key: str):
    with db.connect() as conn:
        row = conn.execute("select value from settings where key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value) -> None:
    with db.connect() as conn:
        conn.execute("insert into settings (key, value) values (%s, %s) "
                     "on conflict (key) do update set value = excluded.value", (key, json.dumps(value)))


def _bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(), config.TZ)
    return start, start + timedelta(days=1)


def _at(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(d, time(h, m), config.TZ)


# The day's own todos: due that day (plus anything overdue, on today), and those done that day.
DAY_TODOS_SQL = (
    "select * from todos where (status = 'open' and (due_on = %(d)s or (%(is_today)s and due_on < %(d)s))) "
    "or (status = 'done' and done_at >= %(lo)s and done_at < %(hi)s) "
    "order by status desc, due_on, priority, created_at")


def _day_todos_args(d: date) -> dict:
    lo, hi = _bounds(d)
    return {"d": d, "is_today": d == datetime.now(config.TZ).date(), "lo": lo, "hi": hi}


def day_todos(d: date) -> list[dict]:
    with db.connect() as conn:
        return conn.execute(DAY_TODOS_SQL, _day_todos_args(d)).fetchall()


def load_day(d: date) -> dict:
    """Everything the day page needs, fetched in one pipelined round trip."""
    lo, hi = _bounds(d)
    with db.connect() as conn:
        with conn.pipeline():
            q_settings = conn.execute("select key, value from settings")
            q_cals = conn.execute("select * from calendars order by is_primary desc, summary")
            q_events = conn.execute(
                "select e.*, c.summary as calendar, c.color from events e join calendars c on c.id = e.calendar_id "
                "where c.synced and e.start_at < %s and e.end_at > %s order by e.all_day desc, e.start_at",
                (hi, lo))
            q_blocks = conn.execute(
                "select b.*, t.title from blocks b join todos t on t.id = b.todo_id "
                "where b.plan_date = %s and b.status = 'proposed' order by b.start_at", (d,))
            q_todos = conn.execute(
                "select * from todos where status = 'open' order by due_on nulls last, priority, created_at")
            q_entries = conn.execute(
                "select id, kind, source, body, occurred_at, meta from raw_entries "
                "where occurred_at >= %s and occurred_at < %s order by occurred_at", (lo, hi))
            q_day_todos = conn.execute(DAY_TODOS_SQL, _day_todos_args(d))
            q_people = conn.execute(journal.DAY_PEOPLE_SQL, (lo, hi))
            # Entry counts for the week strip (Mon–Sun around the day).
            w_lo = _bounds(d - timedelta(days=d.weekday()))[0]
            q_week = conn.execute(
                "select (occurred_at at time zone %s)::date as day, count(*) as n from raw_entries "
                "where occurred_at >= %s and occurred_at < %s group by 1",
                (str(config.TZ), w_lo, w_lo + timedelta(days=7)))
        settings = {r["key"]: r["value"] for r in q_settings.fetchall()}
        return {
            "day_todos": q_day_todos.fetchall(),
            "week": {r["day"]: r["n"] for r in q_week.fetchall()},
            "settings": settings,
            "calendars": q_cals.fetchall(),
            "events": q_events.fetchall(),
            "blocks": q_blocks.fetchall(),
            "todos": q_todos.fetchall(),
            "entries": (entries := q_entries.fetchall()),
            "meetings": journal.meetings_from(q_people.fetchall(),
                                              next((e for e in entries if e["meta"].get("type") == "daily"), None)),
        }


def make_plan(d: date) -> int:
    """Replace the day's proposed blocks with a fresh plan. Returns the number of blocks proposed."""
    day = load_day(d)
    s = day["settings"]
    busy = [(e["start_at"], e["end_at"]) for e in day["events"] if not e["all_day"] and not e["transparent"]]
    planned = {e["lifeos_block"] for e in day["events"] if e["lifeos_block"]}
    with db.connect() as conn:
        already = {r["todo_id"] for r in conn.execute(
            "select todo_id from blocks where plan_date = %s and status = 'accepted' and id = any(%s)",
            (d, list(planned)))}
    todos = [t for t in day["todos"] if t["id"] not in already]
    now = datetime.now(config.TZ)
    gaps = planner.free_gaps(_at(d, s["day_start"]), _at(d, s["day_end"]), busy,
                             now=now if d == now.date() else None)
    slots = planner.plan(todos, gaps, d, default_estimate=int(s["default_estimate_min"]))
    with db.connect() as conn, conn.transaction():
        conn.execute("delete from blocks where plan_date = %s and status = 'proposed'", (d,))
        conn.cursor().executemany(
            "insert into blocks (todo_id, plan_date, start_at, end_at) values (%s, %s, %s, %s)",
            [(sl.todo_id, d, sl.start, sl.end) for sl in slots])
    return len(slots)


def block_calendar(conn) -> str | None:
    """Calendar that accepted blocks are written to: the setting, else my primary calendar."""
    s = get_settings(conn)
    if s.get("block_calendar_id"):
        return s["block_calendar_id"]
    row = conn.execute("select id from calendars where is_primary and access_role in ('owner', 'writer')").fetchone()
    return row["id"] if row else None


def accept(block_ids: list[int]) -> int:
    with db.connect() as conn:
        cal = block_calendar(conn)
        blocks = conn.execute(
            "select b.*, t.title from blocks b join todos t on t.id = b.todo_id "
            "where b.id = any(%s) and b.status = 'proposed'", (block_ids,)).fetchall()
    if not cal:
        raise RuntimeError("No calendar to write to: connect Google Calendar first")
    for b in blocks:
        event_id = google.write_block(b, b["title"], cal)
        with db.connect() as conn:
            conn.execute("update blocks set status = 'accepted', calendar_id = %s, event_id = %s where id = %s",
                         (cal, event_id, b["id"]))
    return len(blocks)


def dismiss(block_id: int) -> None:
    with db.connect() as conn:
        conn.execute("update blocks set status = 'dismissed' where id = %s and status = 'proposed'", (block_id,))


# --- Month view ---------------------------------------------------------------------------------

def month_grid(year: int, month: int) -> list[date]:
    """Monday-first weeks covering the month (5 or 6 rows)."""
    first = date(year, month, 1)
    start = first - timedelta(days=first.weekday())
    nxt = date(year + month // 12, month % 12 + 1, 1)
    end = nxt + timedelta(days=(7 - nxt.weekday()) % 7)
    return [start + timedelta(days=i) for i in range((end - start).days)]


def load_month(year: int, month: int) -> dict:
    """What the month calendar shows per day: events, todos due, birthdays, and whether there's a log."""
    from .people import next_birthday

    days_ = month_grid(year, month)
    lo, hi = _bounds(days_[0])[0], _bounds(days_[-1])[1]
    with db.connect() as conn:
        with conn.pipeline():
            q_events = conn.execute(
                "select e.summary, e.start_at, e.end_at, e.all_day, c.color from events e "
                "join calendars c on c.id = e.calendar_id "
                "where c.synced and e.start_at < %s and e.end_at > %s order by e.all_day desc, e.start_at", (hi, lo))
            q_todos = conn.execute(
                "select id, title, due_on, status, priority from todos where due_on between %s and %s "
                "and status <> 'dropped' order by priority, id", (days_[0], days_[-1]))
            q_logs = conn.execute(
                "select (occurred_at at time zone %s)::date as day, count(*) as n from raw_entries "
                "where occurred_at >= %s and occurred_at < %s group by 1", (str(config.TZ), lo, hi))
            q_bdays = conn.execute(
                "select id, name, birth_month, birth_day, birth_year from people "
                "where kind = 'person' and birth_month is not null and birth_day is not null")
        events, todos, logs, bdays = q_events.fetchall(), q_todos.fetchall(), q_logs.fetchall(), q_bdays.fetchall()
    cells = {d: {"events": [], "todos": [], "birthdays": [], "logs": 0} for d in days_}
    for e in events:
        s, t = e["start_at"].astimezone(config.TZ), e["end_at"].astimezone(config.TZ)
        if e["all_day"]:
            s_day, e_day = e["start_at"].date(), e["end_at"].date() - timedelta(days=1)
        else:
            s_day, e_day = s.date(), (t - timedelta(seconds=1)).date()
        d = max(s_day, days_[0])
        while d <= min(e_day, days_[-1]):
            cells[d]["events"].append({**e, "time": None if e["all_day"] or d != s_day else s.strftime("%H:%M"),
                                       "multi": s_day != e_day})
            d += timedelta(days=1)
    for t in todos:
        cells[t["due_on"]]["todos"].append(t)
    for r in logs:
        if r["day"] in cells:
            cells[r["day"]]["logs"] = r["n"]
    for p in bdays:
        for y in {days_[0].year, days_[-1].year}:
            b = next_birthday(p["birth_month"], p["birth_day"], date(y, 1, 1))
            if b in cells and b.year == y:
                cells[b]["birthdays"].append({**p, "turns": y - p["birth_year"] if p["birth_year"] else None})
    weeks = [days_[i:i + 7] for i in range(0, len(days_), 7)]
    return {"weeks": weeks, "cells": cells}
