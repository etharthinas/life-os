from app.obsidian import parse_birthday, person_names, split_frontmatter, wikilinks


def test_frontmatter_daily():
    fm, body = split_frontmatter('---\nDate: 2026-10-14\nPeople:\n  - "[[여재욱]]"\n  - "[[양기서]]"\n---\n## 사건\n')
    assert fm["Date"] == "2026-10-14"
    assert person_names(fm["People"]) == ["여재욱", "양기서"]
    assert body == "## 사건\n"


def test_frontmatter_missing_or_broken():
    assert split_frontmatter("# title\n") == ({}, "# title\n")
    fm, _ = split_frontmatter("---\na: [unclosed\n---\nx")
    assert "_unparsed" in fm


def test_wikilinks():
    links, embeds = wikilinks("see [[A]] and [[Folder/B|alias]] [[C#Heading]] ![[img 1.png]] [[A]]")
    assert links == ["A", "B", "C"]
    assert embeds == ["img 1.png"]


def test_birthday_formats():
    assert parse_birthday("1998-03-07") == (1998, 3, 7)
    assert parse_birthday("1998.3.7") == (1998, 3, 7)
    assert parse_birthday("3월 7일") == (None, 3, 7)
    assert parse_birthday("03-07") == (None, 3, 7)
    assert parse_birthday("") == (None, None, None)
    assert parse_birthday("someday") == (None, None, None)
    assert parse_birthday("2000-13-01") == (None, None, None)


def test_strip_template():
    from app.obsidian import strip_template

    note = ("---\nDate: 2026-10-14\n---\n« [[2026.10.13]] · [[2026.10.15]] »\n\n지난 오늘: [[2025.10.14]] · [[2024.10.14]]\n\n"
            "## 사건\n1. 출근\n\n## 감사\n\n## 기도\n- a\n### 묵상\n\n# 생각\n")
    assert strip_template(note) == "---\nDate: 2026-10-14\n---\n1. 출근\n\n## 기도\n- a\n"
    assert strip_template("# Title\n## Part\nbody") == "# Title\n## Part\nbody"
    assert strip_template("[[2026.10.13]] 에 이어서") == "[[2026.10.13]] 에 이어서"
