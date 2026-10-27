from datetime import date

from app.capture import parse_quick_todo

THU = date(2026, 10, 22)  # a Thursday


def test_plain_title():
    q = parse_quick_todo("call the bank", THU)
    assert (q.title, q.due_on, q.priority, q.estimate_min, q.project) == ("call the bank", None, 2, None, None)


def test_trailing_modifiers_any_order():
    q = parse_quick_todo("buy gift for mom fri !1 30m #family", THU)
    assert q.title == "buy gift for mom"
    assert q.due_on == date(2026, 10, 23)
    assert (q.priority, q.estimate_min, q.project) == (1, 30, "family")


def test_same_weekday_means_next_week():
    assert parse_quick_todo("x thu", THU).due_on == date(2026, 10, 29)


def test_korean_and_relative():
    assert parse_quick_todo("장보기 내일", THU).due_on == date(2026, 10, 23)
    assert parse_quick_todo("보고서 금", THU).due_on == date(2026, 10, 23)


def test_month_day_rolls_to_next_year():
    assert parse_quick_todo("x 1/5", THU).due_on == date(2027, 1, 5)
    assert parse_quick_todo("x 2026-12-01 2h", THU).due_on == date(2026, 12, 1)


def test_words_inside_title_untouched():
    q = parse_quick_todo("ask mon about today plan", THU)
    assert q.title == "ask mon about today plan"  # 'plan' stops the scan


def test_invalid_date_stays_in_title():
    assert parse_quick_todo("x 2/30", THU).title == "x 2/30"


def test_single_word_is_title():
    assert parse_quick_todo("tomorrow", THU).title == "tomorrow"
