"""Reading the journals' text: frontmatter (a daily note's `People:` list), [[links]] and ![[embeds]],
birthdays as written, and the daily-note template's boilerplate."""

import re
import unicodedata
from datetime import date, datetime

import yaml

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
WIKILINK = re.compile(r"(!?)\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")

def nfc(s: str) -> str:
    """Names are compared in NFC (text from macOS can arrive as NFD)."""
    return unicodedata.normalize("NFC", s)

def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v

def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m[1]) or {}
    except yaml.YAMLError:
        return {"_unparsed": m[1]}, text[m.end():]
    return (_jsonable(fm) if isinstance(fm, dict) else {"_unparsed": m[1]}), text[m.end():]

# The daily-note template's boilerplate: "« [[yesterday]] · [[tomorrow]] »", "지난 오늘: [[a year ago]] · …",
# and the "## 사건" heading every day starts with.
_DATE_LINK = r"\[\[\d{4}\.\d{2}\.\d{2}(?:\|[^\]]*)?\]\]"
TEMPLATE_LINE = re.compile(rf"^(?:«?\s*{_DATE_LINK}(?:\s*·\s*{_DATE_LINK})*\s*»?|지난\s*오늘\s*:.*|#+\s*사건\s*)$")
HEADING = re.compile(r"^(#{1,6})\s")

def strip_template(text: str) -> str:
    """The text (frontmatter kept) without template boilerplate and without empty headings: a
    heading followed by nothing but blank lines before the next heading of its level (or higher)."""
    fm = FRONTMATTER.match(text)
    head, body = (fm[0], text[fm.end():]) if fm else ("", text)
    lines = [l for l in body.split("\n") if not TEMPLATE_LINE.match(l.strip())]
    keep = []
    for i, l in enumerate(lines):
        h = HEADING.match(l)
        if h:
            nxt = next((x for x in lines[i + 1:] if x.strip()), None)
            nh = HEADING.match(nxt) if nxt is not None else None
            if nxt is None or (nh and len(nh[1]) <= len(h[1])):
                continue
        keep.append(l)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip("\n")
    return head + body + ("\n" if body and text.endswith("\n") else "")

def wikilinks(text: str) -> tuple[list[str], list[str]]:
    """Returns (links, embeds). Targets are bare note/file names (folder prefix dropped)."""
    links, embeds = [], []
    for bang, target in WIKILINK.findall(text):
        name = nfc(target.strip().rsplit("/", 1)[-1])
        (embeds if bang else links).append(name)
    return sorted(set(links)), list(dict.fromkeys(embeds))  # embeds keep their order

def parse_birthday(raw) -> tuple[int | None, int | None, int | None]:
    """'1998-03-07', '1998.3.7', '03-07', '3월 7일', date objects → (year, month, day)."""
    if not raw:
        return None, None, None
    s = str(raw).strip()
    if m := re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    elif m := re.search(r"(\d{1,2})\s*월\s*(\d{1,2})", s) or re.fullmatch(r"(\d{1,2})[-./](\d{1,2})", s):
        y, mo, d = None, int(m[1]), int(m[2])
    else:
        return None, None, None
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None, None, None
    return y, mo, d

def person_names(fm_people) -> list[str]:
    """Daily `People:` is a list of "[[Name]]" strings."""
    if not isinstance(fm_people, list):
        return []
    return [nfc(n.strip()) for item in fm_people for _, n in WIKILINK.findall(str(item))]
