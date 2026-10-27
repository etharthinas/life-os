"""Phone contacts, through vCards (.vcf): what iPhone, iCloud and Google Contacts export and import.

Import fills in people's phones, emails and birthdays (matched by phone number, then by name when
the name is unique); contacts that match no one wait for you to pick which to add. Export gives a
vCard per person (opens "Add to Contacts" on a phone) or everyone in one file.
"""

import json
import quopri
import re
from datetime import date

from . import db
from .obsidian import nfc

# --- Parsing ----------------------------------------------------------------------------------------


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in re.split(r"\r\n|\n|\r", text):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper().split(":", 1)[0]:
            lines[-1] = lines[-1][:-1] + raw  # vCard 2.1 soft line break
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m[1] in "nN" else m[1], v)


def _birthday(v: str) -> tuple[int | None, int | None, int | None]:
    v = v.strip()
    if m := re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2}).*", v):
        y = int(m[1])
        return (None if y in (1604, 1900) else y), int(m[2]), int(m[3])  # 1604: Apple's "no year"
    if m := re.fullmatch(r"--(\d{2})-?(\d{2})", v):
        return None, int(m[1]), int(m[2])
    return None, None, None


def clean_phone(p) -> str:
    """A number as written in a note: `tel:010…`, `[010…](tel:…)` → the number itself."""
    p = str(p).strip()
    if m := re.search(r"\[([^\]]+)\]\(tel:", p):
        p = m[1]
    return re.sub(r"^tel:", "", p, flags=re.I).strip()


def phone_key(p: str) -> str:
    """Digits only, Korean +82 numbers as 0…, so formats from different places compare equal."""
    d = re.sub(r"\D", "", p)
    if d.startswith("82") and len(d) >= 11:
        d = "0" + d[2:]
    return d


def parse(text: str) -> list[dict]:
    cards, cur = [], None
    for line in _unfold(text):
        if not line.strip():
            continue
        head, _, value = line.partition(":")
        name, *params = head.split(";")
        name = name.split(".")[-1].upper()  # item1.TEL → TEL
        params_u = [x.upper() for x in params]
        if name == "BEGIN" and value.upper() == "VCARD":
            cur = {"phones": [], "emails": []}
        elif name == "END" and cur is not None:
            if cur.get("name"):
                cards.append(cur)
            cur = None
        elif cur is not None:
            if any("QUOTED-PRINTABLE" in x for x in params_u):
                value = quopri.decodestring(value).decode("utf-8", "replace")
            value = _unescape(value)
            if name == "FN":
                cur["name"] = nfc(value.strip())
            elif name == "N" and not cur.get("name"):
                family, given, *_ = (value.split(";") + ["", ""])[:2] + [""]
                cur["name"] = nfc((family + given).strip() if re.search("[가-힣]", family + given)
                                  else f"{given} {family}".strip())
            elif name == "TEL" and value.strip():
                cur["phones"].append(value.strip())
            elif name == "EMAIL" and value.strip():
                cur["emails"].append(value.strip())
            elif name == "BDAY":
                cur["birthday"] = _birthday(value)
            elif name == "ORG" and value.strip(";"):
                cur["org"] = value.split(";")[0].strip()
            elif name == "NOTE":
                cur["note"] = value.strip()
            elif name == "UID":
                cur["uid"] = value.strip()
    for i, c in enumerate(cards):
        c.setdefault("uid", f"import-{i}-{c['name']}")
    return cards


# --- Import -----------------------------------------------------------------------------------------

def _merge_contact(old: dict, card: dict) -> dict:
    out = dict(old or {})
    for k in ("phones", "emails"):
        seen, merged = set(), []
        for v in (out.get(k) or []) + card[k]:
            key = phone_key(v) if k == "phones" else v.lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(v)
        out[k] = merged
    for k in ("org", "uid"):
        if card.get(k) and not out.get(k):
            out[k] = card[k]
    return out


def import_cards(cards: list[dict]) -> dict:
    """Merge cards into matching people; the rest are kept for review. Returns counts."""
    with db.connect() as conn:
        people = conn.execute("select id, name, aliases, kind, contact, fields, birth_month from people "
                              "where kind = 'person'").fetchall()
    by_phone, by_name = {}, {}
    for p in people:
        for ph in phones_of(p):
            by_phone.setdefault(phone_key(ph), set()).add(p["id"])
        for n in [p["name"], *p["aliases"]]:
            by_name.setdefault(n, set()).add(p["id"])
    pid_rows = {p["id"]: p for p in people}
    matched, unmatched = [], []
    for c in cards:
        ids = set().union(*(by_phone.get(phone_key(ph), set()) for ph in c["phones"])) if c["phones"] else set()
        if len(ids) != 1:
            ids = by_name.get(c["name"], set())
        (matched if len(ids) == 1 else unmatched).append((next(iter(ids)) if len(ids) == 1 else None, c))
    with db.connect() as conn, conn.transaction():
        for pid, c in matched:
            p = pid_rows[pid]
            y, mo, d = c.get("birthday") or (None, None, None)
            conn.execute(
                "update people set contact = %s, "
                "birth_year = coalesce(birth_year, %s), birth_month = coalesce(birth_month, %s), "
                "birth_day = coalesce(birth_day, %s) where id = %s",
                (json.dumps(_merge_contact(p["contact"], c), ensure_ascii=False),
                 y if p["birth_month"] is None else None, mo, d, pid))
        conn.execute("insert into settings (key, value) values ('contacts_pending', %s) "
                     "on conflict (key) do update set value = excluded.value",
                     (json.dumps([c for _, c in unmatched], ensure_ascii=False, default=list),))
    return {"cards": len(cards), "matched": len(matched), "unmatched": len(unmatched)}


def pending() -> list[dict]:
    with db.connect() as conn:
        row = conn.execute("select value from settings where key = 'contacts_pending'").fetchone()
    return row["value"] if row else []


def add_pending(uids: list[str]) -> int:
    """Add the chosen unmatched contacts as new people."""
    cards = pending()
    chosen = [c for c in cards if c["uid"] in set(uids)]
    with db.connect() as conn, conn.transaction():
        for c in chosen:
            y, mo, d = c.get("birthday") or (None, None, None)
            contact = {k: c[k] for k in ("phones", "emails", "org", "uid") if c.get(k)}
            conn.execute("insert into people (name, about, birth_year, birth_month, birth_day, contact) "
                         "values (%s, %s, %s, %s, %s, %s)",
                         (c["name"], c.get("org"), y, mo, d, json.dumps(contact, ensure_ascii=False)))
        rest = [c for c in cards if c["uid"] not in set(uids)]
        conn.execute("update settings set value = %s where key = 'contacts_pending'",
                     (json.dumps(rest, ensure_ascii=False),))
    return len(chosen)


def dismiss_pending() -> None:
    with db.connect() as conn:
        conn.execute("delete from settings where key = 'contacts_pending'")


# --- Export -----------------------------------------------------------------------------------------

def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def vcard(p: dict, orgs: list[str] = ()) -> str:
    c = p.get("contact") or {}
    phones = phones_of(p)
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{_esc(p['name'])}"]
    if re.fullmatch(r"[가-힣]{2,4}", p["name"]):
        lines.append(f"N:{_esc(p['name'][0])};{_esc(p['name'][1:])};;;")
    else:
        parts = p["name"].rsplit(" ", 1)
        lines.append(f"N:{_esc(parts[-1])};{_esc(parts[0] if len(parts) > 1 else '')};;;")
    lines += [f"TEL;TYPE=CELL:{_esc(x)}" for x in phones]
    lines += [f"EMAIL;TYPE=INTERNET:{_esc(x)}" for x in c.get("emails") or []]
    if p.get("birth_month") and p.get("birth_day"):
        y = p.get("birth_year")
        lines.append(f"BDAY:{y:04d}-{p['birth_month']:02d}-{p['birth_day']:02d}" if y
                     else f"BDAY;X-APPLE-OMIT-YEAR=1604:1604-{p['birth_month']:02d}-{p['birth_day']:02d}")
    if c.get("org") or orgs:
        lines.append(f"ORG:{_esc(c.get('org') or orgs[0])}")
    if p.get("about"):
        lines.append(f"NOTE:{_esc(p['about'])}")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def all_vcards() -> str:
    with db.connect() as conn:
        rows = conn.execute(
            "select p.*, coalesce((select array_agg(g.name order by g.name) from memberships ms "
            "join people g on g.id = ms.group_id where ms.person_id = p.id), '{}') as orgs "
            "from people p where kind = 'person' order by name").fetchall()
    return "".join(vcard(r, r["orgs"]) for r in rows)


def phones_of(p: dict) -> list[str]:
    """Numbers from contacts, then the note's `Phone:` (one or a list), without duplicates."""
    phones = list((p.get("contact") or {}).get("phones") or [])
    f = (p.get("fields") or {}).get("Phone")
    for x in (f if isinstance(f, list) else [f] if f else []):
        x = clean_phone(x)
        if phone_key(x) and phone_key(x) not in {phone_key(y) for y in phones}:
            phones.append(x)
    return phones
