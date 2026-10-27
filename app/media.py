"""Images: ingest (dedupe by hash, EXIF-rotated web-size copies) and rendering note bodies with
Obsidian embeds/links as HTML."""

import hashlib
import io
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
        cls = "mention" if text[start] == "@" else "grp"
        out.append(Markup('<a class="{}" href="/people/{}">{}</a>').format(cls, gid, text[start:end]))
        last = end
    out.append(escape(text[last:]))
    return Markup("").join(out)


def render(text: str, images: dict[str, str], people: dict[str, int], notes: dict[str, int], groups=None) -> Markup:
    """Escape `text` and turn Obsidian embeds into <img> and [[links]] into app links.

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
                out.append(Markup('<a class="embed" href="/media/{0}/orig"><img src="/media/{0}/display" '
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
