"""Day journals: one editable document per day, either an imported Obsidian daily note or one
written here. Quick captures (web, Telegram) are appended to it as timestamped paragraphs.

The text is the source of truth for photos (its ![[embeds]]) and for linked people (its [[links]],
plus a daily note's `People:` frontmatter). People can also be picked in the people picker; those
are kept by id, so two people with the same name stay apart.
"""

import json
from datetime import date, datetime, time, timedelta

from . import capture, config, db, media
from .obsidian import FRONTMATTER, person_names, split_frontmatter, wikilinks


def _bounds(d: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(d, time(), config.TZ)
    return lo, lo + timedelta(days=1)


FIND_SQL = ("select * from raw_entries where meta ->> 'type' = 'daily' and occurred_at >= %s and occurred_at < %s "
            "order by id limit 1")


def find(conn, d: date) -> dict | None:
    return conn.execute(FIND_SQL, _bounds(d)).fetchone()


def _create(conn, d: date, source: str, body: str) -> int:
    return conn.execute(
        "insert into raw_entries (kind, source, body, occurred_at, meta) values ('journal', %s, %s, %s, %s) "
        "returning id",
        (source, body, _bounds(d)[0], json.dumps({"type": "daily", "title": f"{d:%Y.%m.%d}"}))).fetchone()["id"]


def editable_text(conn, entry: dict) -> str:
    """What the editor shows: the text without frontmatter, plus `![[…]]` lines for photos that were
    attached without being embedded (older logs), so saving keeps them."""
    text = split_frontmatter(entry["body"])[1].strip()
    _, embeds = wikilinks(text)
    loose = conn.execute(
        "select m.id, m.name from raw_media rm join media m on m.id = rm.media_id "
        "where rm.raw_id = %s order by rm.pos", (entry["id"],)).fetchall()
    extra = [r["id"] for r in loose if r["name"] not in embeds]
    return "\n".join(x for x in (text, _embed_lines(conn, extra)) if x)


def _embed_lines(conn, media_ids) -> str:
    """`![[name]]` lines for uploaded photos (web uploads get a unique name from their hash)."""
    if not media_ids:
        return ""
    conn.execute("update media set name = left(sha256, 16) || '.' || split_part(content_type, '/', 2) "
                 "where id = any(%s) and name is null", (list(media_ids),))
    names = {r["id"]: r["name"] for r in conn.execute("select id, name from media where id = any(%s)",
                                                      (list(media_ids),))}
    return "\n".join(f"![[{names[i]}]]" for i in media_ids)


def append(text: str, source: str, d: date | None = None, media_ids=()) -> dict:
    """Add a paragraph (and photos) to the day's journal, creating the journal if needed."""
    today = capture.today()
    d = d or today
    with db.connect() as conn, conn.transaction():
        piece = text.strip()
        if d == today and piece:
            piece = f"**{datetime.now(config.TZ):%H:%M}** {piece}"
        return _append(conn, d, source, "\n".join(x for x in (piece, _embed_lines(conn, media_ids)) if x))


def _append(conn, d: date, source: str, piece: str) -> dict:
    doc = find(conn, d)
    if doc:
        body = (doc["body"].rstrip() + "\n\n" + piece).strip()
        conn.execute("update raw_entries set body = %s, updated_at = now() where id = %s", (body, doc["id"]))
        raw_id = doc["id"]
    else:
        body = piece
        raw_id = _create(conn, d, source, body)
    sync(conn, raw_id, body)
    return {"id": raw_id, "body": body}


def merge_logs() -> int:
    """One-off: fold separate log entries (from before journals were one per day) into their day's journal."""
    with db.connect() as conn:
        logs = conn.execute("select * from raw_entries where kind = 'log' order by occurred_at, id").fetchall()
        for log in logs:
            with conn.transaction():
                local = log["occurred_at"].astimezone(config.TZ)
                media_ids = [r["media_id"] for r in conn.execute(
                    "select media_id from raw_media where raw_id = %s order by pos", (log["id"],))]
                text = log["body"].strip()
                piece = "\n".join(x for x in (f"**{local:%H:%M}** {text}" if text else "",
                                              _embed_lines(conn, media_ids)) if x)
                conn.execute("delete from raw_entries where id = %s", (log["id"],))
                if piece:
                    _append(conn, local.date(), log["source"], piece)
    return len(logs)


def strip_templates() -> int:
    """One-off: take the daily-note template's boilerplate and empty headings out of every entry
    (see obsidian.strip_template), and re-derive everyone's people (met vs. only named, names
    written as plain text). Returns entries whose text changed."""
    from .obsidian import strip_template

    with db.connect() as conn:
        rows = conn.execute("select id, body from raw_entries").fetchall()
        n = 0
        for r in rows:
            body = strip_template(r["body"])
            with conn.transaction():
                if body != r["body"]:
                    conn.execute("update raw_entries set body = %s, updated_at = now() where id = %s", (body, r["id"]))
                    n += 1
                sync(conn, r["id"], body)
    return n


def save(raw_id: int, text: str, media_ids=()) -> None:
    """Overwrite an entry's text (its frontmatter, hidden in the editor, is kept as it was)."""
    with db.connect() as conn, conn.transaction():
        row = conn.execute("select body from raw_entries where id = %s for update", (raw_id,)).fetchone()
        if not row:
            raise KeyError(raw_id)
        m = FRONTMATTER.match(row["body"])
        body = (m[0] if m else "") + "\n".join(x for x in (text.strip(), _embed_lines(conn, media_ids)) if x)
        conn.execute("update raw_entries set body = %s, updated_at = now() where id = %s", (body, raw_id))
        sync(conn, raw_id, body)


def save_day(d: date, text: str, source: str, media_ids=()) -> int:
    """Write the day's journal: overwrite it if it exists, else create it."""
    with db.connect() as conn:
        doc = find(conn, d)
    if doc:
        save(doc["id"], text, media_ids)
        return doc["id"]
    if not text.strip() and not media_ids:
        return 0
    with db.connect() as conn, conn.transaction():
        body = "\n".join(x for x in (text.strip(), _embed_lines(conn, media_ids)) if x)
        raw_id = _create(conn, d, source, body)
        sync(conn, raw_id, body)
    return raw_id


def sync(conn, raw_id: int, body: str) -> None:
    """Re-derive linked people and photos from the text (picked people are left alone).

    Who you were with: @Name (or @Group) in the text, and the people in a daily note's `People:`
    list. Someone only named (a [[Name]] link, a name or group written as plain text, a group in
    `People:`) is just mentioned
    (`named_only`): the entry shows up on their page, but it isn't a meeting (no last contact, not
    "together", no group members brought along). A group you were with (written with @, or picked)
    tags its members under the hood (mention rows with via_group), unless it was dissolved for this
    entry: someone was taken out of it, so the rest are tagged one by one instead.
    """
    from .people import all_members, directory, spans_in_text

    fm, text = split_frontmatter(body)
    links, embeds = wikilinks(body)
    meta = conn.execute("select meta from raw_entries where id = %s", (raw_id,)).fetchone()["meta"]
    dissolved = set(meta.get("dissolved_groups") or [])
    # A [[Name]] means the picked person of that name when there is one, else the first such person.
    names = {**directory(), **{r["name"]: r["id"] for r in conn.execute(
        "select p.id, p.name from mentions m join people p on p.id = m.person_id "
        "where m.raw_id = %s and m.picked", (raw_id,))}}
    spans = spans_in_text(text)
    met = {names[n] for n in person_names(fm.get("People")) if n in names}
    met |= {x for st, _, x in spans if text[st] == "@"}
    named = {names[n] for n in wikilinks(text)[0] if n in names} | {x for st, _, x in spans if text[st] != "@"}
    kinds = {r["id"]: r["kind"] for r in conn.execute("select id, kind from people where id = any(%s)",
                                                      (list(met | named),))}
    # A group in `People:` ("[[IVF]]") says some of them were there, not all: named, not brought along.
    fm_groups = {x for x in met if kinds.get(x) == "group"} - {x for st, _, x in spans if text[st] == "@"}
    met -= fm_groups
    named |= fm_groups
    met = {x for x in met if not (kinds.get(x) == "group" and x in dissolved)}
    named = {x for x in named - met if not (kinds.get(x) == "group" and x in dissolved)}
    conn.execute("delete from mentions where raw_id = %s and not picked", (raw_id,))
    rows = [(raw_id, x, False) for x in sorted(met)] + [(raw_id, x, True) for x in sorted(named)]
    if rows:
        conn.cursor().executemany("insert into mentions (raw_id, person_id, named_only) values (%s, %s, %s) "
                                  "on conflict do nothing", rows)
    groups = conn.execute(
        "select m.person_id, m.meeting from mentions m join people p on p.id = m.person_id "
        "where m.raw_id = %s and p.kind = 'group' and not m.named_only", (raw_id,)).fetchall()
    rows = [(raw_id, m, g["person_id"], g["meeting"]) for g in groups for m in all_members(conn, g["person_id"])]
    if rows:
        # Someone only named in the text who came with a group you met was there after all.
        conn.cursor().executemany("insert into mentions (raw_id, person_id, via_group, meeting) "
                                  "values (%s, %s, %s, %s) on conflict (raw_id, person_id) do update "
                                  "set via_group = excluded.via_group, meeting = excluded.meeting, named_only = false "
                                  "where mentions.named_only", rows)
    _sync_media(conn, raw_id, embeds)
    conn.execute("update raw_entries set meta = meta || jsonb_build_object('links', %s::jsonb, 'embeds', %s::jsonb) "
                 "where id = %s", (json.dumps(links, ensure_ascii=False), json.dumps(embeds, ensure_ascii=False), raw_id))
    photo_mentions(conn, raw_id)


def _sync_media(conn, raw_id: int, embeds: list[str]) -> None:
    by_name ={r["name"]: r["id"] for r in conn.execute("select id, name from media where name = any(%s)", (embeds,))}
    conn.execute("delete from raw_media where raw_id = %s", (raw_id,))
    rows = [(raw_id, by_name[e], pos) for pos, e in enumerate(embeds) if e in by_name]
    if rows:
        conn.cursor().executemany("insert into raw_media (raw_id, media_id, pos) values (%s, %s, %s) "
                                  "on conflict do nothing", rows)


# Whose faces are tagged in each day's photos (you, in everything, left out), minus anyone taken
# off that day by hand (`meta.not_there`).
PHOTO_PEOPLE_SQL = """
select distinct rm.raw_id, f.person_id
from raw_media rm
join raw_entries r on r.id = rm.raw_id and r.meta ->> 'type' = 'daily'
join faces f on f.media_id = rm.media_id and f.status in ('auto', 'confirmed')
join people p on p.id = f.person_id and p.kind = 'person'
where (%(raw)s::bigint is null or rm.raw_id = %(raw)s)
  and f.person_id is distinct from (select (value ->> 'person_id')::bigint from settings where key = 'me')
  and not coalesce(r.meta -> 'not_there', '[]') @> to_jsonb(f.person_id)
"""

# Groups (direct memberships), to label people seen together: three colleagues in a photo are that team.
PHOTO_GROUPS_SQL = """
select ms.group_id, ms.person_id, (select count(*) from memberships x where x.group_id = ms.group_id) as size
from memberships ms join people g on g.id = ms.group_id and g.kind = 'group'
where ms.person_id = any(%s)
"""


def photo_groups(present: set[int], members: dict[int, set[int]], size: dict[int, int]) -> dict[int, set[int]]:
    """Groups that the people in a day's photos make up: the group covering the most of them (the
    smaller one on a tie: more specific) takes them, again for the rest, while it's 2 or more."""
    left, out = set(present), {}
    while True:
        best = max(members, key=lambda g: (len(members[g] & left), -size[g], -g), default=None)
        if best is None or len(members[best] & left) < 2:
            return out
        out[best] = members[best] & left
        left -= out[best]


def photo_mentions(conn, raw_id: int | None = None) -> None:
    """Re-derive the photo tags of one journal (or all, after faces were re-tagged): the people in
    its photos, and the groups they make up (a group tagged this way doesn't bring its absent members)."""
    found = conn.execute(PHOTO_PEOPLE_SQL, {"raw": raw_id}).fetchall()
    present: dict[int, set[int]] = {}
    for r in found:
        present.setdefault(r["raw_id"], set()).add(r["person_id"])
    members: dict[int, set[int]] = {}
    size: dict[int, int] = {}
    for r in conn.execute(PHOTO_GROUPS_SQL, (list({r["person_id"] for r in found}),)):
        members.setdefault(r["group_id"], set()).add(r["person_id"])
        size[r["group_id"]] = r["size"]
    hidden = {r["id"]: set(r["meta"].get("not_there") or []) for r in conn.execute(
        "select id, meta from raw_entries where id = any(%s) and meta ? 'not_there'", (list(present),))}
    want = []
    for rid, ps in present.items():
        groups = photo_groups(ps, {g: m for g, m in members.items() if g not in hidden.get(rid, ())}, size)
        want += [(rid, x) for x in [*ps, *groups]]
    args = {"raw": raw_id, "r": [w[0] for w in want], "p": [w[1] for w in want]}
    with conn.transaction():
        conn.execute("delete from mentions m where m.from_photo and not m.picked "
                     "and (%(raw)s::bigint is null or m.raw_id = %(raw)s) "
                     "and (m.raw_id, m.person_id) not in (select * from unnest(%(r)s::bigint[], %(p)s::bigint[]))", args)
        conn.execute("insert into mentions (raw_id, person_id, from_photo) "
                     "select * , true from unnest(%(r)s::bigint[], %(p)s::bigint[]) on conflict (raw_id, person_id) "
                     "do update set from_photo = true, named_only = false where mentions.named_only", args)


# --- People picker --------------------------------------------------------------------------------

def doc_id(d: date, source: str = "web") -> int:
    """The day's journal id, creating an empty journal if there's none yet."""
    with db.connect() as conn, conn.transaction():
        doc = find(conn, d)
        return doc["id"] if doc else _create(conn, d, source, "")


def pick_person(d: date, person_id: int, meeting: int = 0) -> None:
    """Add someone (or a group, with all its members) to one of the day's meetings."""
    raw_id = doc_id(d)
    with db.connect() as conn, conn.transaction():
        # Picking a group again undoes an earlier "dissolve".
        conn.execute("update raw_entries set meta = jsonb_set(meta, '{dissolved_groups}', coalesce((select jsonb_agg(x) "
                     "from jsonb_array_elements(meta -> 'dissolved_groups') x where x <> to_jsonb(%s::bigint)), '[]')) "
                     "where id = %s and meta ? 'dissolved_groups'", (person_id, raw_id))
        conn.execute("update raw_entries set meta = jsonb_set(meta, '{not_there}', coalesce((select jsonb_agg(x) "
                     "from jsonb_array_elements(meta -> 'not_there') x where x <> to_jsonb(%s::bigint)), '[]')) "
                     "where id = %s and meta ? 'not_there'", (person_id, raw_id))
        conn.execute("insert into mentions (raw_id, person_id, picked, meeting) values (%s, %s, true, %s) "
                     "on conflict (raw_id, person_id) do update set picked = true, via_group = null, from_photo = false, named_only = false, "
                     "meeting = excluded.meeting", (raw_id, person_id, meeting))
        conn.execute("delete from mentions where raw_id = %s and via_group = %s", (raw_id, person_id))  # re-expanded below
        doc = conn.execute("select id, body from raw_entries where id = %s", (raw_id,)).fetchone()
        sync(conn, doc["id"], doc["body"])


def unpick_person(d: date, person_id: int) -> None:
    """Take someone off the day. A group goes with the members it brought; someone who came with
    a group dissolves it (the others stay, tagged one by one). Someone [[linked]] in the text
    stays until the link is removed; someone tagged from the photos stays off the day (a group
    found in the photos just loses its label)."""
    from .people import all_members

    with db.connect() as conn, conn.transaction():
        doc = find(conn, d)
        if not doc:
            return
        row = conn.execute("select m.via_group, m.from_photo, p.kind from mentions m join people p on p.id = m.person_id "
                           "where m.raw_id = %s and m.person_id = %s", (doc["id"], person_id)).fetchone()
        if row and row["from_photo"]:  # in the photos, but not someone you were with (or not them)
            conn.execute("update raw_entries set meta = jsonb_set(meta, '{not_there}', "
                         "coalesce(meta -> 'not_there', '[]'::jsonb) || to_jsonb(%s::bigint)) where id = %s",
                         (person_id, doc["id"]))
            conn.execute("delete from mentions where raw_id = %s and person_id = %s", (doc["id"], person_id))
        elif row and row["kind"] == "group":
            _dissolve(conn, doc, person_id, keep=[])
        elif row and row["via_group"]:
            g = row["via_group"]
            _dissolve(conn, doc, g, keep=[m for m in all_members(conn, g) if m != person_id])
        else:
            conn.execute("delete from mentions where raw_id = %s and person_id = %s", (doc["id"], person_id))
        sync(conn, doc["id"], doc["body"])


def _dissolve(conn, doc: dict, group_id: int, keep: list[int]) -> None:
    """The group's tag goes from this entry (and its name in the text won't bring it back); `keep`
    become picked one by one."""
    conn.execute("update raw_entries set meta = jsonb_set(meta, '{dissolved_groups}', "
                 "coalesce(meta -> 'dissolved_groups', '[]'::jsonb) || to_jsonb(%s::bigint)) where id = %s",
                 (group_id, doc["id"]))
    g = conn.execute("select meeting from mentions where raw_id = %s and person_id = %s",
                     (doc["id"], group_id)).fetchone()
    conn.execute("delete from mentions where raw_id = %s and (person_id = %s or via_group = %s)",
                 (doc["id"], group_id, group_id))
    if keep:
        conn.cursor().executemany(
            "insert into mentions (raw_id, person_id, picked, meeting) values (%s, %s, true, %s) "
            "on conflict (raw_id, person_id) do update set picked = true, via_group = null, named_only = false",
            [(doc["id"], m, g["meeting"] if g else 0) for m in keep])


DAY_PEOPLE_SQL = """
select p.id, p.name, p.about, p.kind, ph.sha256 as photo, m.meeting, m.picked, m.via_group, m.from_photo, m.named_only,
       m.via_group is null as direct,
       case when m.from_photo then (select array_agg(ms.group_id) from memberships ms where ms.person_id = p.id) end as orgs
from raw_entries r
join mentions m on m.raw_id = r.id
join people p on p.id = m.person_id
left join media ph on ph.id = p.photo_id
where r.meta ->> 'type' = 'daily' and r.occurred_at >= %s and r.occurred_at < %s
order by m.meeting, p.kind desc, p.name
"""


def day_people(d: date) -> list[dict]:
    """The day's meetings, each with who was there: groups first (with the members they brought, or
    for a group found in the photos, the members in them), then people tagged one by one; and,
    apart, who the text only names (`mentioned`). There's always at least one (untitled) meeting."""
    with db.connect() as conn:
        with conn.pipeline():
            q_rows = conn.execute(DAY_PEOPLE_SQL, _bounds(d))
            q_doc = conn.execute(FIND_SQL, _bounds(d))
        return meetings_from(q_rows.fetchall(), q_doc.fetchone())


def meetings_from(rows: list[dict], doc: dict | None) -> list[dict]:
    """`day_people` from rows already fetched (DAY_PEOPLE_SQL) and the day's journal (or None)."""
    titles = list((doc["meta"].get("meetings") if doc else None) or [""])
    n = max([len(titles) - 1] + [r["meeting"] for r in rows])
    titles += [""] * (n + 1 - len(titles))
    meetings = [{"n": i, "title": t, "people": [], "mentioned": []} for i, t in enumerate(titles)]
    for i, mt in enumerate(meetings):
        mt["mentioned"] = [r for r in rows if r["meeting"] == i and r["named_only"]]
        mine = [r for r in rows if r["meeting"] == i and not r["named_only"]]
        groups = {r["id"]: {**r, "members": []} for r in mine if r["kind"] == "group"}
        mt["people"] = list(groups.values())
        for r in mine:
            if r["kind"] == "group":
                continue
            # Seen in the photos with others from a group (tagged from the photos too): under its label.
            seen_with = r["from_photo"] and next((g for g in r["orgs"] or [] if groups.get(g, {}).get("from_photo")), None)
            if not r["direct"] and r["via_group"] in groups:
                groups[r["via_group"]]["members"].append(r)
            elif seen_with:
                groups[seen_with]["members"].append(r)
            else:
                mt["people"].append(r)
    return meetings


def everyone(meetings: list[dict]) -> list[dict]:
    """All the people (and groups) across a day's meetings."""
    return [x for mt in meetings for p in mt["people"] for x in [p, *p.get("members", [])]]


def _set_meetings(conn, raw_id: int, titles: list[str]) -> None:
    conn.execute("update raw_entries set meta = jsonb_set(meta, '{meetings}', %s::jsonb) where id = %s",
                 (json.dumps(titles, ensure_ascii=False), raw_id))


def add_meeting(d: date) -> None:
    raw_id = doc_id(d)
    titles = [mt["title"] for mt in day_people(d)] + [""]
    with db.connect() as conn:
        _set_meetings(conn, raw_id, titles)


def rename_meeting(d: date, n: int, title: str) -> None:
    raw_id = doc_id(d)
    titles = [mt["title"] for mt in day_people(d)]
    if 0 <= n < len(titles):
        titles[n] = title.strip()
        with db.connect() as conn:
            _set_meetings(conn, raw_id, titles)


def remove_meeting(d: date, n: int) -> None:
    """Drop a meeting: the people picked for it come off the day; later meetings move up."""
    meetings = day_people(d)
    if not (0 <= n < len(meetings)) or len(meetings) == 1:
        return
    with db.connect() as conn, conn.transaction():
        doc = find(conn, d)
        conn.execute("delete from mentions where raw_id = %s and meeting = %s and (picked or via_group is not null)",
                     (doc["id"], n))
        conn.execute("update mentions set meeting = 0 where raw_id = %s and meeting = %s", (doc["id"], n))
        conn.execute("update mentions set meeting = meeting - 1 where raw_id = %s and meeting > %s", (doc["id"], n))
        _set_meetings(conn, doc["id"], [mt["title"] for mt in meetings if mt["n"] != n])
        sync(conn, doc["id"], doc["body"])



# --- Reading back ---------------------------------------------------------------------------------

TIMELINE_SQL = """
select r.id, r.body, r.occurred_at, r.meta, r.kind, r.source,
       coalesce((select json_agg(json_build_object('id', p.id, 'name', p.name, 'kind', p.kind) order by p.kind desc, p.name)
                 from mentions m join people p on p.id = m.person_id
                 where m.raw_id = r.id and not m.named_only and m.via_group is null), '[]') as people
from raw_entries r
where r.meta ->> 'type' = 'daily' and (%(before)s::timestamptz is null or r.occurred_at < %(before)s)
order by r.occurred_at desc, r.id desc
limit %(limit)s
"""


def timeline(before: datetime | None = None, limit: int = 15) -> tuple[list[dict], datetime | None]:
    """Day journals, newest first, before `before`: (entries, where the next page starts or None)."""
    with db.connect() as conn:
        rows = conn.execute(TIMELINE_SQL, {"before": before, "limit": limit}).fetchall()
    return rows, rows[-1]["occurred_at"] if len(rows) == limit else None


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["strip"]:
        print("cleaned", strip_templates(), "entries")
