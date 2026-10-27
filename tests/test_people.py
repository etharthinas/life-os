from datetime import date, datetime

from app import config
from app.media import plain, render
from app.people import annotate, initials, next_birthday


def test_next_birthday():
    today = date(2026, 10, 26)
    assert next_birthday(10, 26, today) == today
    assert next_birthday(10, 25, today) == date(2027, 10, 25)
    assert next_birthday(2, 29, today) == date(2027, 3, 1)


def test_annotate():
    seen = datetime(2026, 9, 1, 23, 0, tzinfo=config.TZ)
    p = annotate({"last_seen": seen, "keep_in_touch_days": 30, "birth_year": 2002, "birth_month": 11,
                  "birth_day": 2}, date(2026, 10, 26))
    assert p["days_since"] == 55 and p["overdue"]
    assert p["birthday_in"] == 7 and p["turns"] == 24
    assert not annotate({"last_seen": None, "keep_in_touch_days": None}, date(2026, 10, 26))["overdue"]
    assert annotate({"last_seen": None, "keep_in_touch_days": 7}, date(2026, 10, 26))["overdue"]


def test_initials():
    assert initials("공의진") == "의진"
    assert initials("Jane Doe") == "JD"
    assert initials("IVF") == "IV"


def test_render_links_and_embeds():
    html = str(render("<b> with [[공의진|의진]] and [[IVF]] ![[a.jpeg|center|300]] ![[gone.png]] [[Nowhere]]",
                      {"a.jpeg": "f" * 64}, {"공의진": 1}, {"IVF": 9}))
    assert html.startswith("&lt;b&gt; with ")
    assert '<a href="/people/1">의진</a>' in html
    assert '<a href="/notes/9">IVF</a>' in html
    assert f'src="/media/{"f" * 64}/display"' in html
    assert "📎 gone.png" in html
    assert '<span class="link">Nowhere</span>' in html


def test_plain():
    assert plain("met [[A|Al]] ![[x.png]] at [[Folder/B]]") == "met Al  at B"


def test_render_markdown():
    from app.media import render_markdown
    html = str(render_markdown("## 사건\n1. went **out**\n- [x] done\n\n\n\n> quote [[A]]\nplain", {}, {"A": 3}, {}))
    assert '<span class="md-h md-h2">사건</span>' in html
    assert '<span class="md-mark">1.</span><span>went <strong>out</strong></span>' in html
    assert '<span class="md-mark">☑</span>' in html
    assert html.count("md-gap") == 1
    assert '<span class="md-quote">quote <a href="/people/3">A</a></span>' in html


def test_at_mentions_and_groups_in_text(monkeypatch):
    from app import people
    monkeypatch.setattr(people, "_directory", (1e18, {"김민수": 1, "Aaron Liu": 2, "김민": 3, "GH-19": 9, "GH": 8}))
    monkeypatch.setattr(people, "_groups", (1e18, {"GH-19": 9, "GH": 8}))
    monkeypatch.setattr(people, "_mention_re", (0, None))
    assert people.spans_in_text("@김민수랑 밥") == [(0, 4, 1)]
    assert people.spans_in_text("@Aaron Liu와") == [(0, 10, 2)]
    assert people.spans_in_text("mail me@김민수") == []
    assert people.spans_in_text("GH-19랑, GHz") == [(0, 5, 9)]


def test_photo_groups():
    from app.journal import photo_groups

    members = {10: {1, 2, 3, 9}, 11: {1, 2}, 12: {4, 5}, 13: {6}}
    size = {10: 4, 11: 2, 12: 2, 13: 1}
    # 1 and 2 are in both 10 and 11: the smaller group wins the tie; one person alone is no group.
    assert photo_groups({1, 2, 4, 5, 6}, members, size) == {11: {1, 2}, 12: {4, 5}}
    assert photo_groups({1, 2, 3}, members, size) == {10: {1, 2, 3}}
    assert photo_groups({1}, members, size) == {}
