"""People: list sections (birthdays, catch up), person pages, and rendering entries with links."""

import collections
import re
import time
from datetime import date, datetime

from . import config, db, media
from .obsidian import nfc

# --- Name directory -----------------------------------------------------------------------------
# name/alias → person id, cached in-process so resolving [[links]] costs no DB round trip.

_directory: tuple[float, dict[str, int]] = (0.0, {})


def directory() -> dict[str, int]:
    global _directory
    if time.monotonic() - _directory[0] > 300:
        names = {}
        with db.connect() as conn:
            for r in conn.execute("select id, name, aliases from people order by id"):
                for n in [r["name"], *r["aliases"]]:
                    names.setdefault(n, r["id"])
        _directory = (time.monotonic(), names)
    return _directory[1]


def forget_directory() -> None:
    global _directory, _groups, _people_names
    _directory = (0.0, {})
    _groups = (0.0, {})
    _people_names = (0.0, {}, {})


# group name/alias → group id, for spotting groups written as plain text ("GH-19랑 저녁").
_groups: tuple[float, dict[str, int]] = (0.0, {})


def group_directory() -> dict[str, int]:
    global _groups
    if time.monotonic() - _groups[0] > 300:
        names = {}
        with db.connect() as conn:
            for r in conn.execute("select id, name, aliases from people where kind = 'group' order by id"):
                for n in [r["name"], *r["aliases"]]:
                    if len(n) >= 2:
                        names.setdefault(n, r["id"])
        _groups = (time.monotonic(), names)
    return _groups[1]


_mention_re: tuple[int, re.Pattern | None] = (0, None)


def at_mentions(text: str) -> list[tuple[int, int, int]]:
    """(start, end, id) of @mentions: "@" + a person's or group's name or nickname (the longest that
    fits, so "@김민수랑" finds 김민수 and "@Aaron Liu" works)."""
    global _mention_re
    names = directory()
    if _mention_re[0] != id(names):
        alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n)
        _mention_re = (id(names), re.compile(rf"(?<![0-9A-Za-z._%+-])@({alts})(?![0-9A-Za-z_])") if alts else None)
    pat = _mention_re[1]
    return [(m.start(), m.end(), names[m[1]]) for m in pat.finditer(text)] if pat else []


# Korean given names that are everyday words too ("은혜를 받았다"): only the full name counts.
COMMON_WORDS = {"은혜", "사랑", "지혜", "소망", "기쁨", "하늘", "한결", "다솜", "보람", "슬기", "나라", "가람", "바다",
                "우리", "하나", "한솔", "은총", "믿음", "진리", "평강", "온유", "이슬", "새롬", "찬양", "주은", "예은"}
# What may follow a given name: a nickname ending, a title or a particle ("민준이형", "지현이랑", "민수 씨").
_AFTER_GIVEN = ("이형|이누나|이오빠|이언니|이가|이는|이도|이랑|이와|이를|이한테|이에게|이네|이의|이만|이까지|이처럼|이" 
                "|형|누나|오빠|언니|선배|후배|쌤|씨|님|가|는|은|도|랑|와|과|를|을|한테|에게|네|의|만|까지|처럼|부터")

_people_names: tuple[float, dict[str, int], dict[str, int]] = (0.0, {}, {})
_names_re: tuple[int, re.Pattern | None, re.Pattern | None] = (0, None, None)


def person_name_directory() -> tuple[dict[str, int], dict[str, int]]:
    """Names a person goes by in plain text: (full names and nicknames, given names). A
    three-syllable Korean name's given name (김민수 → 민수) counts when no one else has it, it isn't
    anyone's name or nickname, and it isn't an everyday word."""
    global _people_names
    if time.monotonic() - _people_names[0] > 300:
        full, every, given = {}, set(), collections.defaultdict(set)
        with db.connect() as conn:
            for r in conn.execute("select id, name, kind, aliases from people order by id"):
                every.update([r["name"], *r["aliases"]])
                if r["kind"] != "person":
                    continue
                for n in [r["name"], *r["aliases"]]:
                    if len(n) >= 2:
                        full.setdefault(n, r["id"])
                if len(r["name"]) == 3 and all("가" <= c <= "힣" for c in r["name"]):
                    given[r["name"][1:]].add(r["id"])
        given = {g: next(iter(ids)) for g, ids in given.items()
                 if len(ids) == 1 and g not in every and g not in COMMON_WORDS}
        _people_names = (time.monotonic(), full, given)
    return _people_names[1], _people_names[2]


def names_in_text(text: str) -> list[tuple[int, int, int]]:
    """(start, end, person id) of people named in plain text, without [[ ]] or @ ("민수랑 점심",
    "Aaron said"). Whole names only: not glued to letters before (nor Latin letters after); a
    given name needs a space, punctuation, a nickname ending or a particle after it."""
    global _names_re
    full, given = person_name_directory()
    if _names_re[0] != id(full):
        alt = lambda ns: "|".join(re.escape(n) for n in sorted(ns, key=len, reverse=True))
        _names_re = (id(full),
                     re.compile(rf"(?<![0-9A-Za-z가-힣_@-])({alt(full)})(?![0-9A-Za-z_])") if full else None,
                     re.compile(rf"(?<![0-9A-Za-z가-힣_@-])({alt(given)})(?=$|[^0-9A-Za-z가-힣_]|(?:{_AFTER_GIVEN}))")
                     if given else None)
    out = []
    for pat, names in ((_names_re[1], full), (_names_re[2], given)):
        if pat:
            out += [(m.start(), m.end(), names[m[1]]) for m in pat.finditer(text)]
    return out


def spans_in_text(text: str) -> list[tuple[int, int, int]]:
    """@mentions, and group and people names written as plain text, in order, not overlapping."""
    out, taken = [], []
    for st, en, i in sorted(at_mentions(text) + groups_in_text(text) + names_in_text(text), key=lambda s: (s[0], -s[1])):
        if not any(st < e and en > s for s, e in taken):
            out.append((st, en, i))
            taken.append((st, en))
    return out


def groups_in_text(text: str) -> list[tuple[int, int, int]]:
    """(start, end, group id) of group names written in the text. Case-sensitive, whole names: not
    glued to other letters before, nor to Latin letters, digits or "-" after (Korean particles are
    fine: "GH-19랑"). Longest name wins ("GH-19" over "GH")."""
    names = group_directory()
    if not names:
        return []
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pat = re.compile(rf"(?<![0-9A-Za-z가-힣_-])({alts})(?![0-9A-Za-z_-])")
    return [(m.start(), m.end(), names[m[1]]) for m in pat.finditer(text)]


# --- Dates --------------------------------------------------------------------------------------

def next_birthday(month: int, day: int, today: date) -> date:
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:  # Feb 29 in a common year
            d = date(year, 3, 1)
        if d >= today:
            return d
    raise AssertionError


def annotate(p: dict, today: date) -> dict:
    """Adds days_since (last contact), overdue, birthday (next date), birthday_in, turns."""
    seen = p.get("last_seen")
    p["days_since"] = (today - seen.astimezone(config.TZ).date()).days if seen else None
    target = p.get("keep_in_touch_days")
    p["overdue"] = bool(target) and (p["days_since"] is None or p["days_since"] > target)
    p["birthday"] = p["birthday_in"] = p["turns"] = None
    if p.get("birth_month") and p.get("birth_day"):
        p["birthday"] = next_birthday(p["birth_month"], p["birth_day"], today)
        p["birthday_in"] = (p["birthday"] - today).days
        if p.get("birth_year"):
            p["turns"] = p["birthday"].year - p["birth_year"]
    return p


def initials(name: str) -> str:
    """Korean 3-syllable names show the given name (김민수 → 민수), others the first letters."""
    if len(name) == 3 and all("가" <= c <= "힣" for c in name):
        return name[1:]
    words = name.split()
    return "".join(w[0] for w in words[:2]).upper() if len(words) > 1 else name[:2]


def hue(name: str) -> int:
    return sum(map(ord, name)) * 47 % 360


# --- Queries ------------------------------------------------------------------------------------

# Each person with their organizations; filtered by name, kind and organization.
LIST_SQL = """
select p.id, p.name, p.about, p.kind, p.aliases, p.birth_year, p.birth_month, p.birth_day, p.keep_in_touch_days,
       p.contact, ph.sha256 as photo, count(m.raw_id) as mentions,
       max(r.occurred_at) filter (where not m.named_only) as last_seen,
       coalesce((select json_agg(json_build_object('id', g.id, 'name', g.name) order by g.name)
                 from memberships ms join people g on g.id = ms.group_id where ms.person_id = p.id), '[]') as orgs,
       (select count(*) from memberships ms where ms.group_id = p.id) as members
from people p
left join media ph on ph.id = p.photo_id
left join mentions m on m.person_id = p.id
left join raw_entries r on r.id = m.raw_id
where (%(q)s = '' or p.name ilike '%%' || %(q)s || '%%' or array_to_string(p.aliases, ' ') ilike '%%' || %(q)s || '%%'
       or p.about ilike '%%' || %(q)s || '%%')
  and (%(kind)s = '' or p.kind = %(kind)s)
  and (%(org)s::bigint is null or exists (select 1 from memberships ms where ms.person_id = p.id and ms.group_id = %(org)s)
       or p.id = %(org)s)
group by p.id, ph.sha256
order by max(r.occurred_at) desc nulls last, p.name
"""


def create(name: str, kind: str = "person", about: str = "") -> dict:
    """A new person. Names needn't be unique: `about` tells same-named people apart."""
    name = nfc(name.strip())
    if not name or kind not in ("person", "group"):
        raise ValueError("name and kind")
    with db.connect() as conn:
        row = conn.execute("insert into people (name, kind, about) values (%s, %s, %s) returning *",
                           (name, kind, about.strip() or None)).fetchone()
        if kind == "group":
            add_me(conn, [row["id"]])
    forget_directory()
    return row


SUGGEST_SQL = """
select p.id, p.name, p.about, p.kind, p.aliases, ph.sha256 as photo,
       max(r.occurred_at) filter (where not m.named_only) as last_seen,
       (select count(*) from people x where x.name = p.name) as same_name
from people p
left join media ph on ph.id = p.photo_id
left join mentions m on m.person_id = p.id
left join raw_entries r on r.id = m.raw_id
where (%(q)s = '' or p.name ilike '%%' || %(q)s || '%%' or array_to_string(p.aliases, ' ') ilike '%%' || %(q)s || '%%')
  and p.id <> all(%(exclude)s)
  and (%(kind)s = '' or p.kind = %(kind)s)
group by p.id, ph.sha256
order by p.name = %(q)s desc, max(r.occurred_at) desc nulls last, p.name
limit 8
"""


def suggest(q: str, exclude=(), kind: str = "") -> list[dict]:
    """People for the picker: name/nickname matches, most recently seen first (all recent ones for q = "")."""
    with db.connect() as conn:
        return conn.execute(SUGGEST_SQL, {"q": nfc(q), "exclude": list(exclude), "kind": kind}).fetchall()


ORGS_SQL = """
select g.id, g.name, count(ms.person_id) as n from people g join memberships ms on ms.group_id = g.id
group by g.id order by n desc, g.name
"""

SORTS = {
    "recent": None,  # LIST_SQL's order
    "name": lambda p: p["name"],
    "entries": lambda p: -p["mentions"],
    "birthday": lambda p: (p["birthday_in"] is None, p["birthday_in"] or 0),
}
SHOWS = {
    "": lambda p: True,
    "birthday": lambda p: p["birthday"] is not None,
    "catchup": lambda p: p["overdue"],
    "quiet": lambda p: p["kind"] == "person" and (p["days_since"] is None or p["days_since"] > 180),
    "nophoto": lambda p: p["kind"] == "person" and not p["photo"],
    "contact": lambda p: bool(p["contact"].get("phones") or p["contact"].get("emails")),
}


def list_people(q: str = "", kind: str = "", org: int | None = None, show: str = "", sort: str = "recent") -> dict:
    today = datetime.now(config.TZ).date()
    with db.connect() as conn:
        with conn.pipeline():
            q_rows = conn.execute(LIST_SQL, {"q": nfc(q), "kind": kind, "org": org})
            q_orgs = conn.execute(ORGS_SQL)
        rows = [annotate(r, today) for r in q_rows.fetchall()]
        orgs = q_orgs.fetchall()
    rows = [p for p in rows if SHOWS.get(show, SHOWS[""])(p)]
    if SORTS.get(sort):
        rows.sort(key=SORTS[sort])
    upcoming = sorted((p for p in rows if p["birthday_in"] is not None and p["birthday_in"] <= 30),
                      key=lambda p: p["birthday_in"])
    overdue = sorted((p for p in rows if p["overdue"]),
                     key=lambda p: -(p["days_since"] or 10**6) / p["keep_in_touch_days"])
    return {"people": rows, "upcoming": upcoming, "overdue": overdue, "orgs": orgs}


# A group's members, and a person's organizations.
MEMBERS_SQL = """
select p.id, p.name, p.about, ph.sha256 as photo
from memberships ms join people p on p.id = ms.person_id left join media ph on ph.id = p.photo_id
where ms.group_id = %(id)s order by p.name
"""

PERSON_ORGS_SQL = """
select g.id, g.name from memberships ms join people g on g.id = ms.group_id
where ms.person_id = %(id)s order by g.name
"""


def person_orgs(person_id: int) -> list[dict]:
    with db.connect() as conn:
        return conn.execute(PERSON_ORGS_SQL, {"id": person_id}).fetchall()


def join_org(person_id: int, group_id: int) -> None:
    with db.connect() as conn:
        conn.execute("insert into memberships (person_id, group_id) select %s, id from people "
                     "where id = %s and kind = 'group' and id <> %s on conflict do nothing",
                     (person_id, group_id, person_id))


def add_me(conn, group_ids: list[int] | None = None) -> None:
    """You're in every group (or these): the diary's owner, once you've said who you are."""
    conn.execute("insert into memberships (person_id, group_id) "
                 "select (s.value ->> 'person_id')::bigint, g.id from settings s join people g on g.kind = 'group' "
                 "where s.key = 'me' and (%(g)s::bigint[] is null or g.id = any(%(g)s)) "
                 "and g.id <> (s.value ->> 'person_id')::bigint on conflict do nothing", {"g": group_ids})


def leave_org(person_id: int, group_id: int) -> None:
    with db.connect() as conn:
        conn.execute("delete from memberships where person_id = %s and group_id = %s", (person_id, group_id))


# Photos in entries that mention them, and photos their face is tagged in.
PHOTOS_SQL = """
select m.id, m.sha256, m.taken_at, rm.raw_id, r.occurred_at
from raw_media rm
join media m on m.id = rm.media_id
join raw_entries r on r.id = rm.raw_id
where rm.raw_id in (select raw_id from mentions where person_id = %(id)s
                    union
                    select rm2.raw_id from faces f join raw_media rm2 on rm2.media_id = f.media_id
                    where f.person_id = %(id)s and f.status in ('auto', 'confirmed'))
order by r.occurred_at desc, rm.raw_id, rm.pos
"""

# People who show up in the same entries most often.
OFTEN_WITH_SQL = """
select p.id, p.name, ph.sha256 as photo, count(*) as n
from mentions a
join mentions b on b.raw_id = a.raw_id and b.person_id <> a.person_id and b.via_group is null and b.meeting = a.meeting
                and not b.named_only
join people p on p.id = b.person_id
left join media ph on ph.id = p.photo_id
where a.person_id = %(id)s and not a.named_only and p.kind = 'person'
group by p.id, ph.sha256 order by n desc, p.name limit 12
"""


def load_person(person_id: int, limit: int | None = 60) -> dict | None:
    """Everything the person page needs, in one pipelined round trip. `limit`: timeline entries."""
    from .faces import OF_PERSON_SQL

    args = {"id": person_id, "limit": limit}
    with db.connect() as conn:
        with conn.pipeline():
            q_p = conn.execute(
                "select p.*, ph.sha256 as photo, "
                "(select count(*) from mentions where person_id = p.id) as mentions, "
                "(select max(r.occurred_at) from mentions m join raw_entries r on r.id = m.raw_id "
                " where m.person_id = p.id and not m.named_only) as last_seen "
                "from people p left join media ph on ph.id = p.photo_id where p.id = %(id)s", args)
            q_t = conn.execute(
                "select r.id, r.body, r.occurred_at, r.meta, r.source from mentions m "
                "join raw_entries r on r.id = m.raw_id "
                "where m.person_id = %(id)s order by r.occurred_at desc, r.id desc limit %(limit)s", args)
            q_photos = conn.execute(PHOTOS_SQL, args)
            q_with = conn.execute(OFTEN_WITH_SQL, args)
            q_members = conn.execute(MEMBERS_SQL, args)
            q_faces = conn.execute(OF_PERSON_SQL, (person_id,))
            q_orgs = conn.execute(PERSON_ORGS_SQL, args)
            q_me = conn.execute("select value from settings where key = 'me'")
        p = q_p.fetchone()
        if not p:
            return None
        photos = q_photos.fetchall()
        timeline = q_t.fetchall()
        often_with, members, my_faces = q_with.fetchall(), q_members.fetchall(), q_faces.fetchall()
        orgs, me = q_orgs.fetchall(), q_me.fetchone()
    annotate(p, datetime.now(config.TZ).date())
    by_raw: dict[int, list] = {}
    for ph in photos:
        by_raw.setdefault(ph["raw_id"], []).append(ph)
    for n in timeline:
        n["photos"] = by_raw.get(n["id"], [])
    # The same image can be embedded in several notes; the gallery shows it once.
    seen, gallery = set(), []
    for ph in photos:
        if ph["id"] not in seen:
            seen.add(ph["id"])
            gallery.append(ph)
    return {"p": p, "timeline": timeline, "photos": gallery, "often_with": often_with, "members": members, "my_faces": my_faces,
            "orgs": orgs, "me": me["value"] if me else None}


def gallery(person_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(PHOTOS_SQL, {"id": person_id}).fetchall()
    seen = set()
    return [r for r in rows if not (r["id"] in seen or seen.add(r["id"]))]


def render_entries(entries: list[dict]) -> None:
    """Sets entry["html"]: the body (frontmatter dropped) with images and [[links]] resolved."""
    from .obsidian import split_frontmatter

    if not entries:
        return
    titles = sorted({m[2].strip().rsplit("/", 1)[-1] for e in entries for m in media.LINK.finditer(e["body"])
                     if not m[1]})
    with db.connect() as conn:
        with conn.pipeline():
            q_img = conn.execute(
                "select rm.raw_id, m.name, m.sha256 from raw_media rm join media m on m.id = rm.media_id "
                "where rm.raw_id = any(%s) order by rm.pos", ([e["id"] for e in entries],))
            q_notes = conn.execute(
                "select id, meta ->> 'title' as title from raw_entries "
                "where source = 'obsidian' and meta ->> 'title' = any(%s)", (titles,))
            q_picked = conn.execute(
                "select m.raw_id, p.id, p.name from mentions m join people p on p.id = m.person_id "
                "where m.raw_id = any(%s) and m.picked", ([e["id"] for e in entries],))
        images = q_img.fetchall()
        notes = {r["title"]: r["id"] for r in q_notes.fetchall()}
        picked = q_picked.fetchall()
    names = directory()
    for e in entries:
        # [[Name]] is the person picked for this entry when several share the name.
        people = names | {r["name"]: r["id"] for r in picked if r["raw_id"] == e["id"]}
        mine = [i for i in images if i["raw_id"] == e["id"]]
        by_name = {i["name"]: i["sha256"] for i in mine if i["name"]}
        body = split_frontmatter(e["body"])[1].strip()
        html = media.render_markdown(body, by_name, people, notes, spans_in_text)
        # Photos attached in the app (no embed in the text) go after the text.
        extra = [i["sha256"] for i in mine if not i["name"] or i["name"] not in body]
        e["html"], e["extra_photos"] = html, extra


# --- Graph ----------------------------------------------------------------------------------------

GRAPH_NODES_SQL = """
select p.id, p.name, p.kind, ph.sha256 as photo,
       (select count(*) from mentions m where m.person_id = p.id) as mentions,
       coalesce((select array_agg(ms.group_id) from memberships ms where ms.person_id = p.id), '{}') as orgs
from people p left join media ph on ph.id = p.photo_id
"""

# Two people in the same entry, twice or more: the strongest ties.
GRAPH_TIES_SQL = """
select a.person_id as source, b.person_id as target, count(*) as weight
from mentions a
join mentions b on b.raw_id = a.raw_id and a.person_id < b.person_id
                and a.via_group is null and b.via_group is null and a.meeting = b.meeting
                and not a.named_only and not b.named_only
join people pa on pa.id = a.person_id and pa.kind = 'person'
join people pb on pb.id = b.person_id and pb.kind = 'person'
group by 1, 2 having count(*) >= 2
order by weight desc limit %s
"""


def graph(max_ties: int = 700) -> dict:
    """Nodes (people and groups) and links (memberships, and ties from shared entries)."""
    with db.connect() as conn:
        with conn.pipeline():
            q_nodes = conn.execute(GRAPH_NODES_SQL)
            q_ties = conn.execute(GRAPH_TIES_SQL, (max_ties,))
        nodes, ties = q_nodes.fetchall(), q_ties.fetchall()
    size = collections.Counter(g for n in nodes for g in n["orgs"])
    links = [{"source": n["id"], "target": g, "kind": "org"} for n in nodes for g in n["orgs"]]
    links += [{**t, "kind": "tie"} for t in ties]
    linked = {l["source"] for l in links} | {l["target"] for l in links}
    out = []
    for n in nodes:
        if n["id"] not in linked:
            continue  # no one to connect to: left out to keep the picture readable
        main = max(n["orgs"], key=lambda g: size[g], default=n["id"] if n["kind"] == "group" else None)
        out.append({"id": n["id"], "name": n["name"], "kind": n["kind"], "photo": n["photo"],
                    "mentions": n["mentions"], "org": main})
    return {"nodes": out, "links": links}


# --- Groups: subgroups, members ---------------------------------------------------------------------

TREE_SQL = """
with recursive tree(id) as (
    select %(g)s::bigint union select c.id from people c join tree t on c.parent_id = t.id
)
"""


def all_members(conn, group_id: int) -> list[int]:
    """Everyone in the group or any of its subgroups."""
    return [r["person_id"] for r in conn.execute(
        TREE_SQL + "select distinct ms.person_id from memberships ms join people p on p.id = ms.person_id "
                   "where ms.group_id in (select id from tree) and p.kind = 'person'", {"g": group_id})]


GROUP_PAGE_SQL = TREE_SQL + """
select
  (select json_agg(json_build_object('id', c.id, 'name', c.name,
          'n', (select count(*) from memberships ms where ms.group_id = c.id)) order by c.name)
   from people c where c.parent_id = %(g)s) as subgroups,
  (select json_build_object('id', p.id, 'name', p.name) from people g join people p on p.id = g.parent_id
   where g.id = %(g)s) as parent,
  (select count(distinct ms.person_id) from memberships ms where ms.group_id in (select id from tree)) as everyone
"""


def group_page(group_id: int) -> dict:
    with db.connect() as conn:
        r = conn.execute(GROUP_PAGE_SQL, {"g": group_id}).fetchone()
    return {"subgroups": r["subgroups"] or [], "parent": r["parent"], "everyone": r["everyone"]}


def create_subgroup(parent_id: int, name: str) -> dict:
    g = create(name, kind="group")
    with db.connect() as conn:
        conn.execute("update people set parent_id = %s where id = %s", (parent_id, g["id"]))
    return g


def set_parent(group_id: int, parent_id: int | None) -> None:
    """Put a group under another (not under itself or one of its own subgroups)."""
    with db.connect() as conn:
        if parent_id is not None:
            below = {r["id"] for r in conn.execute(TREE_SQL + "select id from tree", {"g": group_id})}
            if parent_id in below:
                raise ValueError("a group can't be inside its own subgroup")
        conn.execute("update people set parent_id = %s where id = %s and kind = 'group'", (parent_id, group_id))
