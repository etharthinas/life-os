from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.planner import free_gaps, plan

KST = ZoneInfo("Asia/Seoul")
D = date(2026, 10, 26)


def at(h, m=0):
    return datetime(2026, 10, 26, h, m, tzinfo=KST)


def todo(id, priority=2, due=None, est=None, created=0):
    return {"id": id, "priority": priority, "due_on": due, "estimate_min": est,
            "created_at": datetime(2026, 1, 1, tzinfo=KST) + timedelta(minutes=created)}


def test_gaps_between_overlapping_events():
    busy = [(at(10), at(11)), (at(10, 30), at(12)), (at(12, 5), at(13))]
    assert free_gaps(at(9), at(18), busy) == [(at(9), at(10)), (at(13), at(18))]


def test_gaps_skip_past_time_rounded_to_quarter_hour():
    assert free_gaps(at(9), at(12), [], now=at(10, 7)) == [(at(10, 15), at(12))]


def test_gaps_ignore_events_outside_window():
    busy = [(at(7), at(8)), (at(20), at(21))]
    assert free_gaps(at(9), at(18), busy) == [(at(9), at(18))]


def test_plan_orders_urgent_then_priority():
    todos = [todo(1, priority=3), todo(2, priority=1), todo(3, priority=3, due=D)]
    slots = plan(todos, [(at(9), at(12))], D)
    assert [s.todo_id for s in slots] == [3, 2, 1]
    assert (slots[0].start, slots[0].end) == (at(9), at(9, 30))
    assert slots[2].end == at(10, 30)


def test_plan_first_fit_and_skip_when_nothing_fits():
    gaps = [(at(9), at(9, 30)), (at(13), at(15))]
    slots = plan([todo(1, est=90), todo(2, est=30, created=1), todo(3, est=180, created=2)], gaps, D)
    assert [(s.todo_id, s.start) for s in slots] == [(1, at(13)), (2, at(9))]
