"""Greedy day planner: place open todos into the free gaps between calendar events.

Pure functions (no DB, no Google), so the logic is easy to test.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

MIN_GAP = timedelta(minutes=15)


@dataclass
class Slot:
    todo_id: int
    start: datetime
    end: datetime


def free_gaps(day_start: datetime, day_end: datetime, busy: list[tuple[datetime, datetime]],
              now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """Free intervals in [day_start, day_end] not covered by busy intervals (and not in the past)."""
    start = max(day_start, _round_up(now)) if now else day_start
    gaps, cursor = [], start
    for b_start, b_end in sorted(busy):
        if b_end <= cursor:
            continue
        if b_start >= day_end:
            break
        if b_start - cursor >= MIN_GAP:
            gaps.append((cursor, min(b_start, day_end)))
        cursor = max(cursor, b_end)
    if day_end - cursor >= MIN_GAP:
        gaps.append((cursor, day_end))
    return gaps


def order_todos(todos: list[dict], plan_date: date) -> list[dict]:
    """Overdue/due-today first, then priority, then nearest due date, then oldest."""
    def key(t):
        due = t.get("due_on")
        urgent = 0 if due and due <= plan_date else 1
        return (urgent, t["priority"], due or date.max, t["created_at"])
    return sorted(todos, key=key)


def plan(todos: list[dict], gaps: list[tuple[datetime, datetime]], plan_date: date,
         default_estimate: int = 30) -> list[Slot]:
    """First-fit: each todo (in order) takes the earliest gap long enough for its estimate."""
    gaps = list(gaps)
    slots = []
    for t in order_todos(todos, plan_date):
        need = timedelta(minutes=t.get("estimate_min") or default_estimate)
        for i, (g_start, g_end) in enumerate(gaps):
            if g_end - g_start >= need:
                slots.append(Slot(t["id"], g_start, g_start + need))
                rest = (g_start + need, g_end)
                if rest[1] - rest[0] >= MIN_GAP:
                    gaps[i] = rest
                else:
                    gaps.pop(i)
                break
    return slots


def _round_up(dt: datetime, minutes: int = 15) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    extra = -dt.minute % minutes
    return dt + timedelta(minutes=extra)
