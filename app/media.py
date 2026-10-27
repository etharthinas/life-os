"""Images: ingest (dedupe by hash, EXIF-rotated web-size copies) and rendering note bodies with
![[embeds]] and [[links]] as HTML."""

import hashlib
import io
import json
import re
from datetime import datetime

from markupsafe import Markup, escape
from PIL import Image, ImageOps

from . import config, db, storage

try:  # HEIC from iPhone. Safari converts uploads to JPEG, but vault files can be HEIC.
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp"}
SIZES = {"display": 1600, "thumb": 480}  # longest side, px
SHA = re.compile(r"[0-9a-f]{64}")


def key(sha: str, variant: str) -> str:
    return f"orig/{sha}" if variant == "orig" else f"{variant}/{sha}.webp"


def _taken_at(img: Image.Image) -> datetime | None:
    exif = img.getexif()
    raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, else DateTime
    try:
        return datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S").replace(tzinfo=config.TZ)
    except ValueError:
        return None


def ingest(data: bytes, name: str | None, source: str) -> int:
    """Store an image (original + display + thumb) and return its media id. Same bytes → same row."""
    sha = hashlib.sha256(data).hexdigest()
    with db.connect() as conn:
        if row := conn.execute("select id from media where sha256 = %s", (sha,)).fetchone():
            return row["id"]
    img = Image.open(io.BytesIO(data))
    content_type = Image.MIME.get(img.format, "application/octet-stream")
    taken = _taken_at(img)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
    st = storage.store()
    st.put(key(sha, "orig"), data, content_type)
    for variant, px in SIZES.items():
        copy = img.copy()
        copy.thumbnail((px, px), Image.LANCZOS)
        out = io.BytesIO()
        copy.save(out, "WEBP", quality=80, method=4)  # no EXIF: derived copies drop GPS etc.
        st.put(key(sha, variant), out.getvalue(), "image/webp")
    with db.connect() as conn:
        return conn.execute(
            "insert into media (sha256, name, content_type, bytes, width, height, taken_at, source) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (sha256) do update set sha256 = excluded.sha256 returning id",
            (sha, name, content_type, len(data), img.width, img.height, taken, source)).fetchone()["id"]


# --- Rendering ----------------------------------------------------------------------------------

LINK = re.compile(r"(!?)\[\[([^\]|#^]+)([#^][^\]|]*)?(?:\|([^\]]*))?\]\]")


def plain(text: str) -> str:
    """Body text for previews: embeds dropped, [[links]] reduced to their label."""
    return LINK.sub(lambda m: "" if m[1] else (m[4] or m[2].rsplit("/", 1)[-1]), text)


def _plain(text: str, groups) -> Markup:
    """Escaped text; @mentions and group names in it (found by `groups(text)`) become links."""
    if not groups:
        return escape(text)
    out, last = [], 0
    for start, end, gid in groups(text):
        out.append(escape(text[last:start]))
        if isinstance(gid, dict) and "cands" in gid:  # a name several people share: who, and who else
            out.append(_who_is(text[start:end], gid))
        elif isinstance(gid, dict):  # a name found in the text, or a word you linked: with a way to undo it
            out.append(_linked(text[start:end], gid))
        else:
            cls = "mention" if text[start] == "@" else "grp"
            out.append(Markup('<a class="{}" href="/people/{}">{}</a>').format(cls, gid, text[start:end]))
        last = end
    out.append(escape(text[last:]))
    return Markup("").join(out)


def _undo(raw: int, text: str, hand: bool) -> Markup:
    """The "it's not a name" buttons of a name's menu."""
    vals = Markup(escape(json.dumps({"text": text}, ensure_ascii=False)))
    out = Markup('<button type="button" class="undo" hx-post="/entries/{}/unlink" hx-vals=\'{}\'>{}</button>').format(
        raw, vals, "Unlink" if hand else "Not a name here")
    if not hand:
        out += Markup('<button type="button" class="undo" hx-post="/entries/{}/never" hx-vals=\'{}\' '
                      'hx-confirm="Never treat “{}” as a name, in any entry?">Never a name</button>').format(raw, vals, text)
    return out


def _linked(label: str, info: dict) -> Markup:
    """A name found in the text (or linked by you): a link, and on hover or tap a menu to undo it."""
    return Markup('<span class="who-is linked" tabindex="0"><a class="grp" href="/people/{}">{}</a>'
                  '<span class="who-pick" role="menu"><span class="q">{}</span>{}</span></span>').format(
        info["id"], label, "Linked by you" if info["hand"] else "Found in the text", _undo(info["raw"], info["text"], info["hand"]))


def _who_is(label: str, amb: dict) -> Markup:
    """An ambiguous given name ("민준"): a link to whoever it was worked out to be (dotted: a guess) or
    a "?" mark, with a picker that opens on hover or tap."""
    pick = next((c for c in amb["cands"] if c["id"] == amb["pick"]), None)
    shown = (Markup('<a href="/people/{}">{}</a>').format(pick["id"], label) if pick
             else Markup('<span>{}</span>').format(label))
    opts = Markup("").join(
        Markup('<button type="button" class="{}" hx-post="/entries/{}/name" hx-vals=\'{}\'>{}<small>{}</small></button>').format(
            "on" if c["id"] == amb["pick"] else "", amb["raw"],
            Markup(escape(json.dumps({"name": amb["name"], "person_id": c["id"]}, ensure_ascii=False))), c["name"], c["sub"])
        for c in amb["cands"])
    state = "you" if amb["how"] == "you" else "guess" if pick else "open"
    return Markup('<span class="who-is {}" tabindex="0">{}<span class="who-pick" role="menu">'
                  '<span class="q">Who is {}?{}</span>{}{}</span></span>').format(
        state, shown, amb["name"], " (guessed)" if state == "guess" else "", opts, _undo(amb["raw"], amb["name"], False))


def render(text: str, images: dict[str, str], people: dict[str, int], notes: dict[str, int], groups=None) -> Markup:
    """Escape `text` and turn ![[embeds]] into <img> and [[links]] into app links.

    images: file name → sha256; people: name/alias → person id; notes: note title → raw id;
    groups: text → [(start, end, group id)] for group names written as plain text.
    """
    out, last = [], 0
    for m in LINK.finditer(text):
        out.append(_plain(text[last:m.start()], groups))
        last = m.end()
        target = m[2].strip().rsplit("/", 1)[-1]
        label = m[4] if m[4] and not m[1] else target
        if m[1]:
            if sha := images.get(target):
                out.append(Markup('<a class="embed" href="/photos/{0}"><img src="/media/{0}/display" '
                                  'loading="lazy" alt="{1}"></a>').format(sha, target))
            else:
                out.append(Markup('<span class="missing">📎 {}</span>').format(target))
        elif target in people:
            out.append(Markup('<a href="/people/{}">{}</a>').format(people[target], label))
        elif target in notes:
            out.append(Markup('<a href="/notes/{}">{}</a>').format(notes[target], label))
        else:
            out.append(Markup('<span class="link">{}</span>').format(label))
    out.append(_plain(text[last:], groups))
    return Markup("").join(out)


HEADING = re.compile(r"(#{1,6})\s+(.*)")
LIST_ITEM = re.compile(r"(\s*)(?:[-*+]\s+\[( |x|X)\]|([-*+])|(\d+)[.)])\s+(.*)")
BOLD = re.compile(r"\*\*(.+?)\*\*")


def render_markdown(text: str, images: dict[str, str], people: dict[str, int], notes: dict[str, int],
                    groups=None) -> Markup:
    """`render`, plus the bits of Markdown the vault uses: headings, lists, checkboxes, bold."""
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    out = []
    for line in text.split("\n"):
        if m := HEADING.fullmatch(line):
            inner = render(m[2], images, people, notes, groups)
            out.append(Markup('<span class="md-h md-h{}">{}</span>').format(min(len(m[1]), 3), inner))
        elif m := LIST_ITEM.fullmatch(line):
            indent, box, bullet, num, rest = m.groups()
            mark = ("☑" if box.lower() == "x" else "☐") if box is not None else ("•" if bullet else f"{num}.")
            out.append(Markup('<span class="md-li" style="--indent: {}">'
                              '<span class="md-mark">{}</span><span>{}</span></span>').format(
                len(indent.expandtabs(2)) // 2, mark, render(rest, images, people, notes, groups)))
        elif line.startswith(">"):
            out.append(Markup('<span class="md-quote">{}</span>').format(
                render(line[1:].strip(), images, people, notes, groups)))
        elif line.strip():
            out.append(Markup('<span class="md-p">{}</span>').format(render(line, images, people, notes, groups)))
        else:
            out.append(Markup('<span class="md-gap"></span>'))
    html = Markup("").join(out)
    return Markup(BOLD.sub(r"<strong>\1</strong>", str(html)))
