"""One-time Obsidian vault import. Run on a Mac that has the vault in iCloud:

    python -m app.obsidian "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Jay's Obsidian"

Each note becomes one raw entry with its text stored verbatim (layer 1, never rewritten).
Entities/ notes with `type: person|group` seed the people table, and every [[link]] or daily
`People:` entry pointing at a person becomes a mention. Safe to re-run: existing notes are skipped.
Embedded images are uploaded to object storage (see app/storage.py) and linked to their notes;
an Entities/ note's first image becomes that person's photo.
"""

import json
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from pathlib import Path

import yaml

from . import config, db, media

SKIP_DIRS = {".obsidian", ".trash", ".smart-env", "Templates"}
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
WIKILINK = re.compile(r"(!?)\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
DAILY_NAME = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")


def nfc(s: str) -> str:
    """macOS file names are NFD; note text (and anything typed) is NFC. Names are compared in NFC."""
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


def occurred_at(path: Path, rel: Path, fm: dict) -> datetime:
    # A daily note's filename is its real date (frontmatter dates can have typos).
    if rel.parts[0] == "Daily" and (m := DAILY_NAME.fullmatch(path.stem)):
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=config.TZ)
    for key in ("Date", "date", "created"):
        if fm.get(key):
            try:
                return datetime.combine(date.fromisoformat(str(fm[key])[:10]), time(), config.TZ)
            except ValueError:
                pass
    st = path.stat()
    return datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_mtime), config.TZ)


def note_type(rel: Path, fm: dict) -> str:
    if fm.get("type"):
        return str(fm["type"])
    return {"Daily": "daily", "Entities": "person"}.get(rel.parts[0], "note")


def scan(vault: Path) -> list[dict]:
    notes = []
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if SKIP_DIRS & set(rel.parts):
            continue
        text = strip_template(path.read_text(encoding="utf-8"))
        fm, _ = split_frontmatter(text)
        links, embeds = wikilinks(text)
        notes.append({
            "path": str(rel),  # as on disk (NFD): the dedupe key of already imported notes
            "body": text,
            "occurred_at": occurred_at(path, rel, fm),
            "meta": {
                "path": str(rel),
                "title": nfc(path.stem),
                "folder": rel.parts[0] if len(rel.parts) > 1 else "",
                "type": note_type(rel, fm),
                "frontmatter": fm,
                "links": links,
                "embeds": embeds,
            },
        })
    return notes


def person_names(fm_people) -> list[str]:
    """Daily `People:` is a list of "[[Name]]" strings."""
    if not isinstance(fm_people, list):
        return []
    return [nfc(n.strip()) for item in fm_people for _, n in WIKILINK.findall(str(item))]


def run(vault: Path) -> None:
    notes = scan(vault)
    print(f"scanned {len(notes)} notes")
    with db.connect() as conn:
        existing = {r["path"]: r for r in conn.execute(
            "select id, meta ->> 'path' as path, body from raw_entries where source = 'obsidian'")}
        new = [n for n in notes if n["path"] not in existing]
        changed = [n["path"] for n in notes if n["path"] in existing and existing[n["path"]]["body"] != n["body"]]
        with conn.transaction():
            conn.cursor().executemany(
                "insert into raw_entries (kind, source, body, occurred_at, meta) "
                "values ('import', 'obsidian', %s, %s, %s)",
                [(n["body"], n["occurred_at"], json.dumps(n["meta"], ensure_ascii=False)) for n in new],
            )
        ids = {r["path"]: r["id"] for r in conn.execute(
            "select id, meta ->> 'path' as path from raw_entries where source = 'obsidian'")}
        print(f"inserted {len(new)}, already present {len(notes) - len(new)}, changed since import {len(changed)}")
        for p in changed[:20]:
            print("  changed (not re-imported, raw entries are immutable):", p)

        # People from Entities/ notes typed person or group.
        people = []
        for n in notes:
            m = n["meta"]
            if m["folder"] != "Entities" or m["type"] not in ("person", "group"):
                continue
            fm = dict(m["frontmatter"])
            aliases = fm.pop("aliases", None) or []
            y, mo, d = parse_birthday(fm.get("Birthday"))
            fm.pop("type", None)
            people.append((m["title"], m["type"], [nfc(str(a)) for a in aliases if a], y, mo, d,
                           json.dumps(fm, ensure_ascii=False), ids[n["path"]]))
        with conn.transaction():
            # A person first seen in a daily `People:` list (or added in the app) gets their note now.
            conn.cursor().executemany(
                "update people set raw_id = %(raw)s where id = (select id from people where name = %(name)s "
                "and raw_id is null order by id limit 1) and not exists (select 1 from people where raw_id = %(raw)s)",
                [{"name": p[0], "raw": p[7]} for p in people])
            conn.cursor().executemany(
                "insert into people (name, kind, aliases, birth_year, birth_month, birth_day, fields, raw_id) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (raw_id) where raw_id is not null do update set name = excluded.name, "
                "kind = excluded.kind, fields = excluded.fields, "
                # Aliases and birthdays can be edited in the app; a re-import only fills gaps.
                "aliases = coalesce((select array_agg(distinct a) from unnest(people.aliases || excluded.aliases) a), '{}'), "
                "birth_year = coalesce(people.birth_year, excluded.birth_year), "
                "birth_month = coalesce(people.birth_month, excluded.birth_month), "
                "birth_day = coalesce(people.birth_day, excluded.birth_day)",
                people,
            )
            # Most people in daily `People:` lists have no note of their own (unresolved links);
            # they still get a person row so their timeline and last contact work.
            listed = sorted({name for n in notes for name in person_names(n["meta"]["frontmatter"].get("People"))})
            conn.cursor().executemany(
                "insert into people (name) select %s where not exists "
                "(select 1 from people where name = %s or %s = any(aliases))",
                [(name, name, name) for name in listed],
            )
            # Organizations from `Associated:` links. Adds only: a re-import brings back a tag that
            # was removed in the app only if the note still lists it.
            conn.execute(ASSOCIATED_SQL)
        by_name = {}
        for r in conn.execute("select id, name, aliases from people"):
            for name in [r["name"], *r["aliases"]]:
                by_name.setdefault(name, r["id"])
        print(f"people: {len(set(by_name.values()))} ({len(people)} with an Entities/ note)")

        import_images(conn, vault, notes, ids)
        # Who's in the new notes, the same way as for any saved entry (met vs. only named, photos).
        from .journal import sync
        from .people import forget_directory

        forget_directory()  # the people just added
        for n in new:
            with conn.transaction():
                sync(conn, ids[n["path"]], n["body"])
        print(f"linked people in {len(new)} new notes")


ASSOCIATED_SQL = r"""
insert into memberships (person_id, group_id)
select distinct p.id, g.id
from people p
cross join lateral jsonb_array_elements_text(
    case jsonb_typeof(p.fields -> 'Associated')
        when 'array' then p.fields -> 'Associated'
        when 'string' then jsonb_build_array(p.fields -> 'Associated')
        else '[]'::jsonb end) a
join people g on g.kind = 'group' and g.id <> p.id
 and regexp_replace(a, '^\[\[([^\]|#]+).*$', '\1') in (select unnest(array[g.name] || g.aliases))
on conflict do nothing
"""


def import_images(conn, vault: Path, notes: list[dict], ids: dict[str, int]) -> None:
    """Upload embedded images (Obsidian resolves embeds by file name) and link them to their notes."""
    files = {}
    for path in vault.rglob("*"):
        if path.suffix.lower() in media.IMAGE_EXT and not SKIP_DIRS & set(path.relative_to(vault).parts):
            files.setdefault(nfc(path.name), path)
    wanted = list(dict.fromkeys(e for n in notes for e in n["meta"]["embeds"] if e in files))
    have = {r["name"]: r["id"] for r in conn.execute("select id, name from media where source = 'obsidian'")}
    todo = [name for name in wanted if name not in have]
    print(f"images: {len(wanted)} embedded, {len(wanted) - len(todo)} already uploaded, uploading {len(todo)}")

    def upload(name):
        try:  # reading an iCloud "dataless" file downloads it first
            return name, media.ingest(files[name].read_bytes(), name, "obsidian")
        except Exception as e:  # one bad file shouldn't stop the import
            print(f"  failed: {name}: {e!r}")
            return name, None

    with ThreadPoolExecutor(6) as pool:
        for i, (name, media_id) in enumerate(pool.map(upload, todo), 1):
            if media_id:
                have[name] = media_id
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}", flush=True)

    links = [(ids[n["path"]], have[e], pos) for n in notes
             for pos, e in enumerate(n["meta"]["embeds"]) if e in have]
    with conn.transaction():
        conn.cursor().executemany("insert into raw_media (raw_id, media_id, pos) values (%s, %s, %s) "
                                  "on conflict do nothing", links)
        conn.execute("update people p set photo_id = (select rm.media_id from raw_media rm "
                     "where rm.raw_id = p.raw_id order by rm.pos limit 1) "
                     "where p.photo_id is null and p.raw_id is not null")
    missing = sorted({e for n in notes for e in n["meta"]["embeds"]
                      if e not in files and e.rsplit(".", 1)[-1].lower() in {x[1:] for x in media.IMAGE_EXT}})
    print(f"image links: {len(links)}" + (f", {len(missing)} embedded images not found in the vault" if missing else ""))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    db.migrate()
    run(Path(sys.argv[1]).expanduser())
