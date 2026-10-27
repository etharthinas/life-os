"""Day journals: one editable document per day, either an imported daily note or one
written here. Quick captures (web, Telegram) are appended to it as timestamped paragraphs.

The text is the source of truth for photos (its ![[embeds]]) and for linked people (its [[links]],
plus a daily note's `People:` frontmatter). People can also be picked in the people picker; those
are kept by id, so two people with the same name stay apart.
"""

import json
from datetime import date, datetime, time, timedelta

from . import capture, config, db, media
from .notetext import FRONTMATTER, person_names, split_frontmatter, wikilinks


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
    (see notetext.strip_template), and re-derive everyone's people (met vs. only named, names
    written as plain text). Returns entries whose text changed."""
    from .notetext import strip_template

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


def clear_day(d: date) -> None:
    """Delete the day's log: its text and photos go. The entry itself goes too unless people are still
    on the day (picked by hand); a blank header alone shouldn't make the day look written in."""
    with db.connect() as conn:
        doc = find(conn, d)
    if not doc:
        return
    save(doc["id"], "")
    with db.connect() as conn:
        conn.execute("delete from raw_entries r where id = %s and not exists "
                     "(select 1 from mentions where raw_id = r.id)", (doc["id"],))


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


# How well each candidate fits the people already there: entries they were in together, groups
# they share (×3), and being there that day already (+100).
FIT_SQL = """
select c.id,
       (select count(*) from mentions a join mentions b on b.raw_id = a.raw_id
        where a.person_id = c.id and b.person_id = any(%(met)s) and b.person_id <> c.id
          and not a.named_only and not b.named_only and a.raw_id <> %(raw)s)
     + 3 * (select count(*) from memberships x join memberships y on y.group_id = x.group_id
            where x.person_id = c.id and y.person_id = any(%(met)s) and y.person_id <> c.id)
     + case when c.id = any(%(met)s) then 100 else 0 end as fit
from unnest(%(cands)s::bigint[]) as c(id)
"""


def guess(fit: dict[int, int]) -> int | None:
    """The clear favourite: someone fits (more than nothing) at least twice as well as anyone else."""
    ranked = sorted(fit.items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] <= 0:
        return None
    if len(ranked) > 1 and ranked[0][1] < 2 * ranked[1][1]:
        return None
    return ranked[0][0]


def resolve_names(conn, raw_id: int, text: str, meta: dict, met: set[int]) -> dict[str, int]:
    """Who each ambiguous given name in the text is: your choice (`meta.names`, how "you"), else the
    clear favourite from the people around them (how "guess"), else unresolved (`meta.ambiguous`,
    listed at /people/ambiguous). Returns name → person id."""
    from .people import ambiguous_in_text, person_name_directory

    found = sorted({n for _, _, n in ambiguous_in_text(text, meta.get("hand_links"), meta.get("not_names") or [])})
    ambiguous = person_name_directory()[2]
    chosen = {n: v for n, v in (meta.get("names") or {}).items() if v.get("how") == "you"}
    names, open_ = {}, []
    for n in found:
        if n in chosen:
            names[n] = chosen[n]
            continue
        fit = {r["id"]: r["fit"] for r in conn.execute(FIT_SQL, {"cands": ambiguous.get(n, []), "met": list(met),
                                                                  "raw": raw_id})}
        if (pick := guess(fit)) is not None:
            names[n] = {"id": pick, "how": "guess"}
        else:
            open_.append(n)
    names |= {n: v for n, v in chosen.items() if n not in names}  # kept even if the text changes back
    if not found and not meta.get("ambiguous") and names == (meta.get("names") or {}):
        return {}
    conn.execute("update raw_entries set meta = meta || jsonb_build_object('names', %s::jsonb, 'ambiguous', %s::jsonb) "
                 "where id = %s", (json.dumps(names), json.dumps(open_, ensure_ascii=False), raw_id))
    meta["names"], meta["ambiguous"] = names, open_
    return {n: v["id"] for n, v in names.items() if n in found}


def choose_name(raw_id: int, name: str, person_id: int | None) -> None:
    """You say who an ambiguous name in this entry is (None: back to guessing)."""
    with db.connect() as conn, conn.transaction():
        row = conn.execute("select body, meta from raw_entries where id = %s for update", (raw_id,)).fetchone()
        if not row:
            raise KeyError(raw_id)
        names = dict(row["meta"].get("names") or {})
        if person_id is None:
            names.pop(name, None)
        else:
            names[name] = {"id": person_id, "how": "you"}
        conn.execute("update raw_entries set meta = jsonb_set(meta, '{names}', %s::jsonb) where id = %s",
                     (json.dumps(names, ensure_ascii=False), raw_id))
        sync(conn, raw_id, row["body"])


def _edit_meta(raw_id: int, change) -> None:
    """Change an entry's meta (`change(meta)` edits it in place), then re-derive its people."""
    with db.connect() as conn, conn.transaction():
        row = conn.execute("select body, meta from raw_entries where id = %s for update", (raw_id,)).fetchone()
        if not row:
            raise KeyError(raw_id)
        meta = dict(row["meta"])
        change(meta)
        conn.execute("update raw_entries set meta = %s where id = %s", (json.dumps(meta, ensure_ascii=False), raw_id))
        sync(conn, raw_id, row["body"])


def link_text(raw_id: int, text: str, person_id: int) -> None:
    """Select a word → Link: everywhere it's written in this entry, it's this person (or group)."""
    def change(meta):
        meta["hand_links"] = {**(meta.get("hand_links") or {}), text: person_id}
        meta["not_names"] = [x for x in meta.get("not_names") or [] if x != text]
    _edit_meta(raw_id, change)


def unlink_text(raw_id: int, text: str) -> None:
    """"Not a name here": a word you linked goes back to plain text; a name found by itself
    ("지연", which is also a word) stops counting in this entry."""
    def change(meta):
        links = dict(meta.get("hand_links") or {})
        if text in links:
            links.pop(text)
            meta["hand_links"] = links
        else:
            meta["not_names"] = sorted({*(meta.get("not_names") or []), text})
        names = dict(meta.get("names") or {})
        names.pop(text, None)
        meta["names"] = names
    _edit_meta(raw_id, change)


def resync_containing(word: str) -> int:
    """Re-derive the people of every entry where `word` is written (after "Never a name")."""
    n = 0
    with db.connect() as conn:
        for r in conn.execute("select id, body from raw_entries where body like '%%' || %s || '%%'", (word,)).fetchall():
            with conn.transaction():
                sync(conn, r["id"], r["body"])
            n += 1
    return n


def resolve_all() -> int:
    """One-off (after names change): work out the ambiguous names in every entry that has one."""
    from .people import ambiguous_in_text

    n = 0
    with db.connect() as conn:
        for r in conn.execute("select id, body from raw_entries").fetchall():
            if ambiguous_in_text(split_frontmatter(r["body"])[1]):
                with conn.transaction():
                    sync(conn, r["id"], r["body"])
                n += 1
    return n


def me_id(conn) -> int | None:
    """The diary's owner (People → "This is me"), who is never tagged in their own entries."""
    r = conn.execute("select (value ->> 'person_id')::bigint as id from settings where key = 'me'").fetchone()
    return r["id"] if r else None


def sync(conn, raw_id: int, body: str) -> None:
    """Re-derive linked people and photos from the text (picked people are left alone).

    Who you were with: @Name (or @Group) in the text, and the people in a daily note's `People:`
    list. Someone only named (a [[Name]] link, a name or group written as plain text, a group in
    `People:`) is just mentioned
    (`named_only`): the entry shows up on their page, but it isn't a meeting (no last contact, not
    "together", no group members brought along). A group you were with (written with @, or picked)
    tags its members under the hood (mention rows with via_group), unless it was taken off this entry
    (`meta.dissolved_groups`). Anyone taken off by hand (`meta.not_there`) stays off, and you (the
    diary's owner) are never tagged in your own entries.
    """
    from .people import all_members, directory, spans_in_text

    fm, text = split_frontmatter(body)
    links, embeds = wikilinks(body)
    meta = conn.execute("select meta from raw_entries where id = %s", (raw_id,)).fetchone()["meta"]
    dissolved = set(meta.get("dissolved_groups") or [])
    away = set(meta.get("not_there") or []) | {me_id(conn)}
    # A [[Name]] means the picked person of that name when there is one, else the first such person.
    names = {**directory(), **{r["name"]: r["id"] for r in conn.execute(
        "select p.id, p.name from mentions m join people p on p.id = m.person_id "
        "where m.raw_id = %s and m.picked", (raw_id,))}}
    # Words you linked to someone by hand, and words that aren't a name here (meta.hand_links, not_names).
    links, not_names = meta.get("hand_links") or {}, meta.get("not_names") or []
    spans = spans_in_text(text, links=links, not_names=not_names)
    met = {names[n] for n in person_names(fm.get("People")) if n in names}
    met |= {x for st, _, x in spans if text[st] == "@"}
    # Given names several people share ("민준"): who, from your choice or the people around them.
    resolved = resolve_names(conn, raw_id, text, meta, met)
    if resolved:
        spans = spans_in_text(text, resolved, links, not_names)
    named = {names[n] for n in wikilinks(text)[0] if n in names} | {x for st, _, x in spans if text[st] != "@"}
    kinds = {r["id"]: r["kind"] for r in conn.execute("select id, kind from people where id = any(%s)",
                                                      (list(met | named),))}
    # A group in `People:` ("[[IVF]]") says some of them were there, not all: named, not brought along.
    fm_groups = {x for x in met if kinds.get(x) == "group"} - {x for st, _, x in spans if text[st] == "@"}
    met -= fm_groups
    named |= fm_groups
    met = {x for x in met - away if not (kinds.get(x) == "group" and x in dissolved)}
    named = {x for x in named - met - away if not (kinds.get(x) == "group" and x in dissolved)}
    # Re-derived tags come back in the meeting they were in (you may have dragged them there).
    was_at = {r["person_id"]: r["meeting"] for r in conn.execute(
        "select person_id, meeting from mentions where raw_id = %s", (raw_id,))}
    conn.execute("delete from mentions where raw_id = %s and (not picked or person_id = any(%s))",
                 (raw_id, [x for x in away if x is not None]))
    rows = [(raw_id, x, False, was_at.get(x, 0)) for x in sorted(met)] + [(raw_id, x, True, was_at.get(x, 0)) for x in sorted(named)]
    if rows:
        conn.cursor().executemany("insert into mentions (raw_id, person_id, named_only, meeting) values (%s, %s, %s, %s) "
                                  "on conflict do nothing", rows)
    groups = conn.execute(
        "select m.person_id, m.meeting from mentions m join people p on p.id = m.person_id "
        "where m.raw_id = %s and p.kind = 'group' and not m.named_only and not m.auto_group", (raw_id,)).fetchall()
    rows = [(raw_id, m, g["person_id"], g["meeting"]) for g in groups for m in all_members(conn, g["person_id"])
            if m not in away]
    if rows:
        # Members already tagged another way (`People:`, the text, photos) are at the group's meeting
        # (unless you put them somewhere yourself); someone only named was there after all.
        conn.cursor().executemany("insert into mentions (raw_id, person_id, via_group, meeting) "
                                  "values (%s, %s, %s, %s) on conflict (raw_id, person_id) do update "
                                  "set via_group = case when mentions.named_only then excluded.via_group "
                                  "else mentions.via_group end, meeting = excluded.meeting, named_only = false "
                                  "where not mentions.picked", rows)
    _sync_media(conn, raw_id, embeds)
    conn.execute("update raw_entries set meta = meta || jsonb_build_object('links', %s::jsonb, 'embeds', %s::jsonb) "
                 "where id = %s", (json.dumps(links, ensure_ascii=False), json.dumps(embeds, ensure_ascii=False), raw_id))
    photo_mentions(conn, raw_id, was_at)


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

# Groups (direct memberships) of the given people, with their size.
GROUPS_OF_SQL = """
select ms.group_id, ms.person_id, (select count(*) from memberships x where x.group_id = ms.group_id) as size
from memberships ms join people g on g.id = ms.group_id and g.kind = 'group'
where ms.person_id = any(%s)
"""


def best_groups(present: set[int], members: dict[int, set[int]], size: dict[int, int]) -> dict[int, set[int]]:
    """Groups that the people at a meeting make up, as tight as possible: the group covering the
    most of them takes them (the smaller one on a tie: GH-19 over GH when it explains them all),
    again for the rest, while it's 2 or more."""
    left, out = set(present), {}
    while True:
        best = max(members, key=lambda g: (len(members[g] & left), -size[g], -g), default=None)
        if best is None or len(members[best] & left) < 2:
            return out
        out[best] = members[best] & left
        left -= out[best]


def photo_mentions(conn, raw_id: int | None = None, was_at: dict[int, int] | None = None) -> None:
    """Re-derive the photo tags of one journal (or all, after faces were re-tagged), then the groups
    everyone there makes up (`auto_groups`). `was_at`: the meeting each person was in (for one journal)."""
    found = conn.execute(PHOTO_PEOPLE_SQL, {"raw": raw_id}).fetchall()
    args = {"raw": raw_id, "r": [r["raw_id"] for r in found], "p": [r["person_id"] for r in found],
            "m": [(was_at or {}).get(r["person_id"], 0) for r in found]}
    with conn.transaction():
        conn.execute("delete from mentions m where m.from_photo and not m.picked "
                     "and (%(raw)s::bigint is null or m.raw_id = %(raw)s) "
                     "and (m.raw_id, m.person_id) not in (select * from unnest(%(r)s::bigint[], %(p)s::bigint[]))", args)
        conn.execute("insert into mentions (raw_id, person_id, meeting, from_photo) "
                     "select *, true from unnest(%(r)s::bigint[], %(p)s::bigint[], %(m)s::int[]) on conflict (raw_id, person_id) "
                     "do update set from_photo = true, named_only = false where mentions.named_only", args)
        auto_groups(conn, raw_id)


# Who was at each meeting of the day journals (one, or all): people and the groups you were with.
MET_SQL = """
select m.raw_id, m.meeting, m.person_id, p.kind, m.via_group
from mentions m join people p on p.id = m.person_id
join raw_entries r on r.id = m.raw_id and r.meta ->> 'type' = 'daily'
where not m.named_only and (not m.auto_group or m.picked) and (%(raw)s::bigint is null or m.raw_id = %(raw)s)
"""


def auto_groups(conn, raw_id: int | None = None) -> None:
    """Label people who were together with the group they make up (`mentions.auto_group`): on 10.7
    여재욱, 양기서, 최한결… are GH-19. A group tagged this way brings no one along; people already under a
    group you were with are left out; a group taken off that day (✕) isn't used again. A group the
    text only names ("[[GH]]" in `People:`) is labelled the same way when they were there. People
    taken out of all groups that day (`meta.ungrouped`, 전체 해제) stay unlabelled."""
    scope = {"raw": raw_id}
    conn.execute("delete from mentions where auto_group and not named_only and not picked "
                 "and (%(raw)s::bigint is null or raw_id = %(raw)s)", scope)
    conn.execute("update mentions set auto_group = false where auto_group and not picked "
                 "and (%(raw)s::bigint is null or raw_id = %(raw)s)", scope)
    rows = conn.execute(MET_SQL, scope).fetchall()
    people = {r["person_id"] for r in rows if r["kind"] == "person"}
    members: dict[int, set[int]] = {}
    size: dict[int, int] = {}
    orgs: dict[int, set[int]] = {}
    for r in conn.execute(GROUPS_OF_SQL, (list(people),)):
        members.setdefault(r["group_id"], set()).add(r["person_id"])
        size[r["group_id"]] = r["size"]
        orgs.setdefault(r["person_id"], set()).add(r["group_id"])
    off, ungrouped = {}, {}
    for r in conn.execute("select id, meta from raw_entries where id = any(%s) and (meta ? 'not_there' "
                          "or meta ? 'dissolved_groups' or meta ? 'ungrouped')", (list({r["raw_id"] for r in rows}),)):
        off[r["id"]] = set(r["meta"].get("not_there") or []) | set(r["meta"].get("dissolved_groups") or [])
        ungrouped[r["id"]] = set(r["meta"].get("ungrouped") or [])
    at: dict[tuple[int, int], dict] = {}
    for r in rows:
        mt = at.setdefault((r["raw_id"], r["meeting"]), {"people": set(), "groups": set(), "via": set()})
        if r["kind"] == "group":
            mt["groups"].add(r["person_id"])
        elif r["via_group"]:
            mt["via"].add(r["person_id"])
        else:
            mt["people"].add(r["person_id"])
    want = []
    for (rid, meeting), mt in at.items():
        loose = {p for p in mt["people"] if not (orgs.get(p, set()) & mt["groups"])}  # not under a group already
        loose -= ungrouped.get(rid, set())  # taken out of every group that day (전체 해제)
        usable = {g: m for g, m in members.items() if g not in off.get(rid, ()) and g not in mt["groups"]}
        want += [(rid, g, meeting) for g in best_groups(loose, usable, size)]
    if want:
        conn.cursor().executemany(
            "insert into mentions (raw_id, person_id, meeting, auto_group) values (%s, %s, %s, true) "
            "on conflict (raw_id, person_id) do update set auto_group = true, meeting = excluded.meeting "
            "where mentions.named_only", want)


# --- People picker --------------------------------------------------------------------------------

def doc_id(d: date, source: str = "web") -> int:
    """The day's journal id, creating an empty journal if there's none yet."""
    with db.connect() as conn, conn.transaction():
        doc = find(conn, d)
        return doc["id"] if doc else _create(conn, d, source, "")


def pick_person(d: date, person_id: int, meeting: int = 0) -> None:
    """Add someone (or a group, with all its members) to one of the day's meetings. Not you. A group
    brings back members taken off the day before."""
    from .people import all_members

    with db.connect() as conn:
        if person_id == me_id(conn):
            return
    raw_id = doc_id(d)
    with db.connect() as conn, conn.transaction():
        back = [person_id, *all_members(conn, person_id)]
        conn.execute("update raw_entries set meta = jsonb_set(meta, '{not_there}', coalesce((select jsonb_agg(x) "
                     "from jsonb_array_elements(meta -> 'not_there') x where not x <@ to_jsonb(%s::bigint[])), '[]')) "
                     "where id = %s and meta ? 'not_there'", (back, raw_id))
        # Picking a group again undoes an earlier "dissolve".
        conn.execute("update raw_entries set meta = jsonb_set(meta, '{dissolved_groups}', coalesce((select jsonb_agg(x) "
                     "from jsonb_array_elements(meta -> 'dissolved_groups') x where x <> to_jsonb(%s::bigint)), '[]')) "
                     "where id = %s and meta ? 'dissolved_groups'", (person_id, raw_id))
        conn.execute("insert into mentions (raw_id, person_id, picked, meeting) values (%s, %s, true, %s) "
                     "on conflict (raw_id, person_id) do update set picked = true, via_group = null, from_photo = false, named_only = false, "
                     "auto_group = false, "
                     "meeting = excluded.meeting", (raw_id, person_id, meeting))
        conn.execute("delete from mentions where raw_id = %s and via_group = %s", (raw_id, person_id))  # re-expanded below
        doc = conn.execute("select id, body from raw_entries where id = %s", (raw_id,)).fetchone()
        sync(conn, doc["id"], doc["body"])


def label_group(d: date, meeting: int, group_id: int, person_ids: list[int] = (), group_ids: list[int] = ()) -> None:
    """묶기: show the people of a meeting under this group's label, by hand. It brings no one else
    along, takes the chosen people from any other label (automatic or 묶기) in that meeting, and
    undoes an earlier ✕ on the group for that day. Groups chosen whole (`group_ids`) give up their
    label: their people (in `person_ids`) go under the new one."""
    raw_id = doc_id(d)
    with db.connect() as conn, conn.transaction():
        doc = conn.execute("select id from raw_entries where id = %s", (raw_id,)).fetchone()
        for g in group_ids:
            if g != group_id:
                _dissolve(conn, doc, g, keep=list(person_ids))
        _drop_from(conn, raw_id, "ungrouped", list(person_ids))
        if person_ids:
            conn.execute("delete from mentions m where m.raw_id = %s and m.meeting = %s and m.auto_group "
                         "and m.person_id <> %s and exists (select 1 from memberships ms "
                         "where ms.group_id = m.person_id and ms.person_id = any(%s))",
                         (raw_id, meeting, group_id, list(person_ids)))
        for key in ("dissolved_groups", "not_there"):
            conn.execute(f"update raw_entries set meta = jsonb_set(meta, '{{{key}}}', coalesce((select jsonb_agg(x) "
                         f"from jsonb_array_elements(meta -> '{key}') x where x <> to_jsonb(%s::bigint)), '[]')) "
                         f"where id = %s and meta ? '{key}'", (group_id, raw_id))
        conn.execute("insert into mentions (raw_id, person_id, picked, auto_group, meeting) values (%s, %s, true, true, %s) "
                     "on conflict (raw_id, person_id) do update set picked = true, auto_group = true, named_only = false, "
                     "via_group = null, meeting = excluded.meeting", (raw_id, group_id, meeting))
        conn.execute("delete from mentions where raw_id = %s and via_group = %s", (raw_id, group_id))
        doc = conn.execute("select body from raw_entries where id = %s", (raw_id,)).fetchone()
        sync(conn, raw_id, doc["body"])


def unpick_person(d: date, person_id: int) -> None:
    """Take someone off the day: they stay off (`meta.not_there`), even if the text, a group or the
    photos would bring them back; the group they were under keeps everyone else. Taking a group off
    takes the members it brought along (not the ones tagged on their own)."""
    with db.connect() as conn, conn.transaction():
        doc = find(conn, d)
        if not doc:
            return
        row = conn.execute("select p.kind from mentions m join people p on p.id = m.person_id "
                           "where m.raw_id = %s and m.person_id = %s", (doc["id"], person_id)).fetchone()
        if row and row["kind"] == "group":
            _dissolve(conn, doc, person_id, keep=[])
        else:
            _freeze_groups_of(conn, doc["id"], person_id)
            conn.execute("update raw_entries set meta = jsonb_set(meta, '{not_there}', "
                         "coalesce(meta -> 'not_there', '[]'::jsonb) || to_jsonb(%s::bigint)) where id = %s",
                         (person_id, doc["id"]))
            conn.execute("delete from mentions where raw_id = %s and person_id = %s", (doc["id"], person_id))
        sync(conn, doc["id"], doc["body"])


def _freeze_groups_of(conn, raw_id: int, person_id: int) -> None:
    """Someone taken out of a group you were with as a whole: from now on that day it's those
    people, not the group. Its tag becomes a label over them (like 묶기) and the people it brought
    are tagged one by one, so someone joining the group later isn't added to that day."""
    from .people import all_members

    whole = conn.execute("select m.person_id from mentions m join people p on p.id = m.person_id "
                         "where m.raw_id = %s and p.kind = 'group' and not m.auto_group and not m.named_only",
                         (raw_id,)).fetchall()
    for g in (r["person_id"] for r in whole):
        if person_id not in all_members(conn, g):
            continue
        conn.execute("update mentions set via_group = null, picked = true where raw_id = %s and via_group = %s", (raw_id, g))
        conn.execute("update mentions set auto_group = true, picked = true where raw_id = %s and person_id = %s", (raw_id, g))


# Groups you were with as a whole (tagged directly, nobody taken out: not a label over some people)
# follow the group: someone who joins it later is at those days too, someone who leaves isn't.
WHOLE_GROUPS_SQL = """
with recursive tree(root, id) as (
    select id, id from people where kind = 'group'
    union select t.root, c.id from people c join tree t on c.parent_id = t.id
), whole as (
    select m.raw_id, m.person_id as g, m.meeting, r.meta from mentions m
    join people gp on gp.id = m.person_id and gp.kind = 'group'
    join raw_entries r on r.id = m.raw_id
    where not m.auto_group and not m.named_only and r.meta ? 'type' and r.meta ->> 'type' = 'daily'
), want as (
    select distinct on (w.raw_id, ms.person_id) w.raw_id, ms.person_id, w.g, w.meeting
    from whole w join tree t on t.root = w.g join memberships ms on ms.group_id = t.id
    join people p on p.id = ms.person_id and p.kind = 'person'
    where ms.person_id is distinct from (select (value ->> 'person_id')::bigint from settings where key = 'me')
      and not coalesce(w.meta -> 'not_there', '[]') @> to_jsonb(ms.person_id)
    order by w.raw_id, ms.person_id, w.g
), added as (
    insert into mentions (raw_id, person_id, via_group, meeting)
    select raw_id, person_id, g, meeting from want
    on conflict (raw_id, person_id) do update set via_group = excluded.via_group, meeting = excluded.meeting,
        named_only = false where mentions.named_only
    returning 1
)
delete from mentions v where v.via_group is not null and not v.picked
  and not exists (select 1 from tree t join memberships ms on ms.group_id = t.id
                  where t.root = v.via_group and ms.person_id = v.person_id)
"""


def follow_whole_groups(conn) -> None:
    """After a group's members changed: `WHOLE_GROUPS_SQL` on every day."""
    conn.execute(WHOLE_GROUPS_SQL)


def _drop_from(conn, raw_id: int, key: str, ids: list[int]) -> None:
    """Take these ids out of the entry's `meta[key]` list."""
    if ids:
        conn.execute(f"update raw_entries set meta = jsonb_set(meta, '{{{key}}}', coalesce((select jsonb_agg(x) "
                     f"from jsonb_array_elements(meta -> '{key}') x where not x <@ to_jsonb(%s::bigint[])), '[]')) "
                     f"where id = %s and meta ? '{key}'", (ids, raw_id))


def ungroup(d: date, group_id: int, member_ids: list[int]) -> None:
    """전체 해제: the group's label comes off and its people stay, each on their own, without being
    put under another group (bigger or smaller) instead (`meta.ungrouped`, until 묶기 labels them)."""
    with db.connect() as conn, conn.transaction():
        doc = find(conn, d)
        if not doc:
            return
        on_day = {r["person_id"] for r in conn.execute("select person_id from mentions where raw_id = %s", (doc["id"],))}
        keep = [m for m in member_ids if m in on_day]
        _dissolve(conn, doc, group_id, keep)
        conn.execute("update raw_entries set meta = jsonb_set(meta, '{ungrouped}', (select coalesce(jsonb_agg(distinct x), '[]') "
                     "from jsonb_array_elements(coalesce(meta -> 'ungrouped', '[]') || to_jsonb(%s::bigint[])) x)) "
                     "where id = %s", (keep, doc["id"]))
        sync(conn, doc["id"], doc["body"])


def move_to_meeting(d: date, ids: list[int], meeting: int) -> None:
    """Drag a chip to another meeting: these people (or a group, with the people under it) go there.
    A group labelled automatically that you moved stays where you put it."""
    meetings = day_people(d)
    if not (0 <= meeting < len(meetings)) or not ids:
        return
    with db.connect() as conn, conn.transaction():
        doc = find(conn, d)
        if not doc:
            return
        conn.execute("update mentions set meeting = %s where raw_id = %s and (person_id = any(%s) or via_group = any(%s))",
                     (meeting, doc["id"], ids, ids))
        conn.execute("update mentions m set picked = true from people p where p.id = m.person_id and p.kind = 'group' "
                     "and m.raw_id = %s and m.person_id = any(%s) and m.auto_group", (doc["id"], ids))
        sync(conn, doc["id"], doc["body"])


def reorder_meetings(d: date, order: list[int]) -> None:
    """Drag meetings into a new order: `order` lists the current meeting numbers as they should go."""
    meetings = day_people(d)
    if sorted(order) != list(range(len(meetings))):
        return
    with db.connect() as conn, conn.transaction():
        doc = find(conn, d)
        if not doc:
            return
        new = {old: i for i, old in enumerate(order)}
        conn.execute("update mentions set meeting = (%s::int[])[meeting + 1] where raw_id = %s",
                     ([new[i] for i in range(len(order))], doc["id"]))
        _set_meetings(conn, doc["id"], [meetings[old]["title"] for old in order])


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
       m.auto_group,
       m.via_group is null as direct,
       (select array_agg(ms.group_id) from memberships ms where ms.person_id = p.id) as orgs
from raw_entries r
join mentions m on m.raw_id = r.id
join people p on p.id = m.person_id
left join media ph on ph.id = p.photo_id
where r.meta ->> 'type' = 'daily' and r.occurred_at >= %s and r.occurred_at < %s
order by m.meeting, p.kind desc, p.name
"""


def day_people(d: date) -> list[dict]:
    """The day's meetings, each with who was there: groups first (with the members they brought, or
    for a group labelled automatically, its members who were there), then people tagged one by one; and,
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
        mt["mentioned"] = [r for r in rows if r["meeting"] == i and r["named_only"] and not r["auto_group"]]
        mine = [r for r in rows if r["meeting"] == i and (not r["named_only"] or r["auto_group"])]
        groups = {r["id"]: {**r, "members": []} for r in mine if r["kind"] == "group"}
        mt["people"] = list(groups.values())
        # A group you were with, then one you grouped them under (묶기), then an automatic label.
        order = sorted(groups, key=lambda g: (groups[g]["auto_group"], not groups[g]["picked"]))
        for r in mine:
            if r["kind"] == "group":
                continue
            # Under the group they came with, else under a group of theirs that's there too (tagged
            # on their own, by @, `People:` or the photos, they're still part of it): 사프 (4).
            under = r["via_group"] if r["via_group"] in groups else next((g for g in order if g in (r["orgs"] or [])), None)
            if under:
                groups[under]["members"].append(r)
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
    elif sys.argv[1:] == ["names"]:
        print("worked out ambiguous names in", resolve_all(), "entries")
