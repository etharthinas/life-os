"""Faces in photos: find them, group similar ones, and work out who they are.

Runs on our own server with OpenCV's YuNet (detection) and SFace (a 128-d face embedding); no photo
leaves it. Who is who comes from three sources, strongest first:
  1. faces you confirmed (and a profile photo with exactly one face in it);
  2. matching groups of similar faces with the people your entries mention next to those photos:
     a group that turns up far more often in someone's entries than in all photos is them (the
     owner of the diary is in everything, so they never match anyone this way);
  3. nearness to the faces already known for a person.
(2) and (3) are `auto` tags, recomputed on every run; your confirmations and rejections stay.
"""

import collections
import io
import json
from functools import cache

from PIL import Image

from . import config, db, journal, media, storage

MIN_FACE_PX = 40         # smaller faces (back rows of group photos) are too blurry to recognize
CLUSTER_SIM = 0.50       # cosine similarity to join a group of similar faces
CORE_SIM = 0.50          # a group's "core": faces this close to its centre (examples of who it is)
MATCH_SIM = 0.45         # a face is tagged as someone if it's this close to their examples (top-3 mean)
MATCH_MARGIN = 0.06      # ... and this much closer than to anyone else
MAX_EXAMPLES = 20        # example faces kept per person


@cache
def _models():
    import cv2

    d = config.FACE_MODEL_DIR
    det = cv2.FaceDetectorYN.create(str(d / "yunet.onnx"), "", (320, 320), 0.8, 0.3, 5000)
    rec = cv2.FaceRecognizerSF.create(str(d / "sface.onnx"), "")
    return det, rec


def available() -> bool:
    return (config.FACE_MODEL_DIR / "yunet.onnx").exists() and (config.FACE_MODEL_DIR / "sface.onnx").exists()


def detect(data: bytes) -> list[tuple[list[float], list[float]]]:
    """(box as fractions [x, y, w, h], unit embedding) for each face big enough to recognize."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    h, w = img.shape[:2]
    det, rec = _models()
    det.setInputSize((w, h))
    _, found = det.detect(img)
    out = []
    for f in [] if found is None else found:
        if min(f[2], f[3]) < MIN_FACE_PX:
            continue
        e = rec.feature(rec.alignCrop(img, f)).flatten()
        out.append(([float(f[0] / w), float(f[1] / h), float(f[2] / w), float(f[3] / h)],
                    (e / np.linalg.norm(e)).tolist()))
    return out


def pending() -> int:
    """Photos not scanned for faces yet (cheap: no models loaded)."""
    with db.connect() as conn:
        return conn.execute("select count(*) as n from media where not faces_scanned and source <> 'avatar'").fetchone()["n"]


def scan_in_subprocess() -> int:
    """`scan` in a short-lived process: the models take ~250 MB, which a long-running process would
    keep for good on a 1 GB server. Returns photos scanned."""
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "app.faces", "scan"], capture_output=True, text=True,
                         timeout=3600, cwd=config.ROOT)
    if out.returncode:
        raise RuntimeError(out.stderr[-1000:])
    return int(out.stdout.strip().splitlines()[-1])


def set_me(person_id: int | None) -> None:
    """Who the diary's owner is: their face (in most photos) gets tagged as them, and they're in every group."""
    with db.connect() as conn:
        if person_id is None:
            conn.execute("delete from settings where key = 'me'")
        else:
            conn.execute("insert into settings (key, value) values ('me', %s) "
                         "on conflict (key) do update set value = excluded.value", (json.dumps({"person_id": person_id}),))
            from .people import add_me
            add_me(conn)
    request_relabel()


def request_relabel() -> None:
    """Ask the worker to re-tag (after a correction), so the web process never loads numpy and co."""
    with db.connect() as conn:
        conn.execute("insert into settings (key, value) values ('faces_dirty', 'true') on conflict (key) do nothing")


def take_relabel_request() -> bool:
    with db.connect() as conn:
        return conn.execute("delete from settings where key = 'faces_dirty' returning key").fetchone() is not None


def scan(limit: int = 200) -> int:
    """Find faces in photos not scanned yet (profile-photo crops excluded). Returns photos scanned."""
    with db.connect() as conn:
        todo = conn.execute("select id, sha256 from media where not faces_scanned and source <> 'avatar' "
                            "order by id limit %s", (limit,)).fetchall()
    st = storage.store()
    for m in todo:
        try:
            found = detect(st.get(media.key(m["sha256"], "display")))
        except KeyError:
            found = []
        with db.connect() as conn, conn.transaction():
            conn.cursor().executemany(
                "insert into faces (media_id, x, y, w, h, embedding) values (%s, %s, %s, %s, %s, %s)",
                [(m["id"], *box, emb) for box, emb in found])
            conn.execute("update media set faces_scanned = true where id = %s", (m["id"],))
    return len(todo)


# --- Grouping and naming --------------------------------------------------------------------------

def _load(conn):
    import numpy as np

    rows = conn.execute("select id, media_id, w * h as area, embedding, cluster, person_id, status "
                        "from faces order by id").fetchall()
    emb = np.array([r["embedding"] for r in rows], dtype=np.float32).reshape(len(rows), -1)
    return rows, emb


def cluster(rows, emb) -> list[int]:
    """Greedy grouping: biggest faces first, each joins the most similar group centroid if close enough."""
    import numpy as np

    labels = [0] * len(rows)
    sums, cents = [], np.zeros((0, emb.shape[1] if len(rows) else 128), np.float32)
    for i in sorted(range(len(rows)), key=lambda i: -rows[i]["area"]):
        if len(cents):
            sims = cents @ emb[i]
            k = int(np.argmax(sims))
            if sims[k] >= CLUSTER_SIM:
                labels[i] = k
                sums[k] += emb[i]
                cents[k] = sums[k] / np.linalg.norm(sums[k])
                continue
        labels[i] = len(sums)
        sums.append(emb[i].copy())
        cents = np.vstack([cents, emb[i][None, :]])
    return labels


def _examples(emb, idx: list[int], limit: int = MAX_EXAMPLES) -> list[int]:
    """The faces most typical of a set (closest to its centre), outliers dropped: a wrongly
    confirmed face doesn't drag the person's look toward someone else."""
    import numpy as np

    if len(idx) <= 2:
        return list(idx)
    c = emb[idx].mean(0)
    c /= np.linalg.norm(c)
    sims = emb[idx] @ c
    order = [idx[j] for j in np.argsort(-sims) if sims[j] >= 0.30]
    return order[:limit] or list(idx[:1])


def _owner(rows, emb, min_share: float = 0.2) -> list[int]:
    """The diary owner's faces: the face found in by far the most photos, and its look-alikes.

    Until you name yourself, these stand in for "me": they stay untagged instead of being matched
    to whoever appears with you most. Empty if no one is in at least `min_share` of the photos."""
    import numpy as np

    media_ids = np.array([r["media_id"] for r in rows])
    n_photos = len(set(media_ids.tolist()))
    best, best_n = -1, 0
    for start in range(0, len(rows), 512):
        sims = emb[start:start + 512] @ emb.T
        for r in range(sims.shape[0]):
            n = len(set(media_ids[sims[r] >= CLUSTER_SIM].tolist()))
            if n > best_n:
                best, best_n = start + r, n
    if best < 0 or best_n < min_share * n_photos:
        return []
    near = np.where(emb @ emb[best] >= CLUSTER_SIM)[0]
    c = emb[near].mean(0)
    c /= np.linalg.norm(c)
    return np.where(emb @ c >= CORE_SIM)[0].tolist()


def relabel(recluster: bool = False) -> dict:
    """Recompute the auto tags (and, with recluster, the groups). Returns counts.

    1. Examples of each person: their confirmed faces (yours, and a profile photo with one face).
    2. People with none yet: the core of the group of similar faces that turns up far more often in
       their entries than in all photos (one group per person, one person per group).
    3. Every other face is tagged as the person whose examples it's clearly closest to, at most
       once per photo per person.
    """
    import numpy as np

    with db.connect() as conn:
        rows, emb = _load(conn)
        if not rows:
            return {"faces": 0}
        if recluster or any(r["cluster"] is None for r in rows):
            labels = cluster(rows, emb)
            with conn.transaction():
                conn.cursor().executemany("update faces set cluster = %s where id = %s",
                                          [(labels[i], r["id"]) for i, r in enumerate(rows)])
        else:
            labels = [r["cluster"] for r in rows]
        mentioned = conn.execute(
            "select distinct rm.media_id, m.person_id from raw_media rm join mentions m on m.raw_id = rm.raw_id "
            "join people p on p.id = m.person_id where p.kind = 'person' and not m.from_photo and not m.named_only").fetchall()
        profile = conn.execute(
            "select p.id as person_id, min(f.id) as face_id from people p join media md on md.id = p.photo_id "
            "join faces f on f.media_id = md.id where md.source <> 'avatar' "
            "group by p.id having count(f.id) = 1").fetchall()
        rejected = {(r["face_id"], r["person_id"]) for r in conn.execute("select * from face_rejections")}
        groups = {r["id"] for r in conn.execute("select id from people where kind = 'group'")}
        me = conn.execute("select value from settings where key = 'me'").fetchone()
    me = (me["value"] or {}).get("person_id") if me else None

    ids = [r["id"] for r in rows]
    status = {r["id"]: r["status"] for r in rows}
    person = {r["id"]: r["person_id"] for r in rows}
    for fid in ids:
        if status[fid] == "auto":
            status[fid], person[fid] = None, None
    for r in profile:
        if status[r["face_id"]] is None and (r["face_id"], r["person_id"]) not in rejected:
            status[r["face_id"]], person[r["face_id"]] = "confirmed", r["person_id"]

    # 1. Examples from confirmed faces.
    confirmed_of = collections.defaultdict(list)
    for i, fid in enumerate(ids):
        if status[fid] == "confirmed" and person[fid] not in groups:
            confirmed_of[person[fid]].append(i)
    examples = {p: _examples(emb, idx) for p, idx in confirmed_of.items()}
    # The owner: unless their faces are mostly confirmed as someone (you named yourself), they're
    # a stand-in person -1 whose matches stay untagged.
    owner = _owner(rows, emb)
    owner_set = set(owner)
    if owner and me:
        # You said who you are: your core faces are confirmed as you (unless you've said otherwise).
        for i in owner:
            if status[ids[i]] is None and (ids[i], me) not in rejected:
                status[ids[i]], person[ids[i]] = "confirmed", me
                confirmed_of[me].append(i)
        examples[me] = _examples(emb, confirmed_of[me])
    elif owner and sum(status[ids[i]] == "confirmed" for i in owner) * 2 < len(owner):
        examples[-1] = _examples(emb, owner)

    # 2. Bootstrap the rest from groups of similar faces + who the entries mention.
    by_cluster = collections.defaultdict(list)
    for i in range(len(rows)):
        by_cluster[labels[i]].append(i)
    claimed = set()  # groups that are mostly someone's confirmed faces
    for k, idx in by_cluster.items():
        conf = [i for i in idx if status[ids[i]] == "confirmed"]
        if conf and len(conf) * 2 >= min(len(idx), 4):
            claimed.add(k)
    cluster_photos = {k: {rows[i]["media_id"] for i in idx} for k, idx in by_cluster.items()}
    # Groups that are mostly the owner's face are never someone else's.
    owner_cluster = {k: sum(i in owner_set for i in idx) * 2 >= len(idx) for k, idx in by_cluster.items()}
    n_photos = len({r["media_id"] for r in rows})
    person_photos = collections.defaultdict(set)
    for r in mentioned:
        person_photos[r["person_id"]].add(r["media_id"])
    refused = collections.Counter()
    index = {fid: i for i, fid in enumerate(ids)}
    for f, p in rejected:
        if f in index:
            refused[(labels[index[f]], p)] += 1
    cands = []
    for p, ph in person_photos.items():
        if p in examples or len(ph) < 2:
            continue
        for k, cp in cluster_photos.items():
            if k in claimed or refused[(k, p)] >= 2 or owner_cluster[k]:
                continue
            n = len(cp & ph)
            if n < 2:
                continue
            lift = (n / len(ph)) / (len(cp) / n_photos)
            if lift >= 2 and n / len(ph) >= 0.3:
                cands.append((n - len(ph) * len(cp) / n_photos, p, k))
    used = set()
    for _, p, k in sorted(cands, reverse=True):
        if p in examples or k in used:
            continue
        idx = by_cluster[k]
        c = emb[idx].mean(0)
        c /= np.linalg.norm(c)
        core = [i for i in idx if emb[i] @ c >= CORE_SIM and status[ids[i]] is None
                and (ids[i], p) not in rejected and i not in owner_set]
        if len({rows[i]["media_id"] for i in core}) >= 2:
            examples[p] = _examples(emb, core)
            used.add(k)

    # 3. Match every untagged face against everyone's examples.
    people_ids = list(examples)
    if people_ids:
        ex_idx = [i for p in people_ids for i in examples[p]]
        ex_owner = np.array([n for n, p in enumerate(people_ids) for _ in examples[p]])
        ex_emb = emb[ex_idx]
        ex_pos = np.array(ex_idx)
        todo = [i for i, fid in enumerate(ids) if status[fid] is None]
        cands = []
        for start in range(0, len(todo), 256):
            chunk = todo[start:start + 256]
            sims = emb[chunk] @ ex_emb.T
            sims[np.array(chunk)[:, None] == ex_pos[None, :]] = -1  # a face isn't evidence for itself
            scores = np.full((len(chunk), len(people_ids)), -1.0, np.float32)
            for n in range(len(people_ids)):
                cols = sims[:, ex_owner == n]
                top = np.sort(cols, axis=1)[:, -3:]
                scores[:, n] = top.mean(1)
            for r, i in enumerate(chunk):
                order = np.argsort(-scores[r])
                s1 = float(scores[r, order[0]])
                s2 = float(scores[r, order[1]]) if len(order) > 1 else -1.0
                p = people_ids[order[0]]
                if p != -1 and s1 >= MATCH_SIM and s1 - s2 >= MATCH_MARGIN and (ids[i], p) not in rejected:
                    cands.append((s1, i, p))
        in_photo = {(rows[i]["media_id"], person[fid]) for i, fid in enumerate(ids)
                    if status[fid] in ("auto", "confirmed")}
        for _, i, p in sorted(cands, reverse=True):  # most certain first; a person once per photo
            key = (rows[i]["media_id"], p)
            if key not in in_photo:
                status[ids[i]], person[ids[i]] = "auto", p
                in_photo.add(key)

    changes = [(person[r["id"]], status[r["id"]], r["id"]) for r in rows
               if (person[r["id"]], status[r["id"]]) != (r["person_id"], r["status"])]
    with db.connect() as conn, conn.transaction():
        conn.cursor().executemany("update faces set person_id = %s, status = %s where id = %s", changes)
        journal.photo_mentions(conn)  # who's in each day's photos → tagged in that day's journal
    auto_avatars()
    counts = collections.Counter(status.values())
    return {"faces": len(rows), "groups": len(by_cluster), "people": len(people_ids), "owner": len(owner),
            "auto": counts["auto"],
            "confirmed": counts["confirmed"], "changed": len(changes)}


# --- Pictures ---------------------------------------------------------------------------------------

def crop(face: dict, size: int = 200, margin: float = 0.35) -> bytes:
    """A square WebP around the face, from the display-size photo."""
    img = Image.open(io.BytesIO(storage.store().get(media.key(face["sha256"], "display")))).convert("RGB")
    W, H = img.size
    cx, cy = (face["x"] + face["w"] / 2) * W, (face["y"] + face["h"] / 2) * H
    # A square around the face, shrunk and slid as needed to stay inside the photo (no black bars).
    half = min(max(face["w"] * W, face["h"] * H) * (0.5 + margin), W / 2, H / 2)
    cx, cy = min(max(cx, half), W - half), min(max(cy, half), H - half)
    box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
    out = io.BytesIO()
    img.crop(box).resize((size, size), Image.LANCZOS).save(out, "WEBP", quality=82)
    return out.getvalue()


FACE_SQL = "select f.*, m.sha256 from faces f join media m on m.id = f.media_id where f.id = %s"


def use_as_profile(person_id: int, face_id: int, auto: bool = False) -> None:
    """Make a face crop this person's profile photo. Auto-picked ones are named so a later
    "not them" on that face can replace it."""
    with db.connect() as conn:
        f = conn.execute(FACE_SQL, (face_id,)).fetchone()
    if not f:
        raise KeyError(face_id)
    media_id = media.ingest(crop(f, size=480, margin=0.6), f"{'auto-' if auto else ''}face-{face_id}.webp", "avatar")
    with db.connect() as conn:
        conn.execute("update people set photo_id = %s where id = %s", (media_id, person_id))


def auto_avatars() -> int:
    """People without a profile photo get their clearest confirmed (else tagged) face; an auto-picked
    one whose face is no longer theirs is picked again."""
    with db.connect() as conn:
        conn.execute(
            "update people p set photo_id = null from media md where md.id = p.photo_id and md.name like 'auto-face-%' "
            "and not exists (select 1 from faces f where f.id = substring(md.name from 'auto-face-(\\d+)')::bigint "
            "and f.person_id = p.id and f.status in ('auto', 'confirmed'))")
        rows = conn.execute(
            "select distinct on (f.person_id) f.person_id, f.id from faces f join people p on p.id = f.person_id "
            "where p.photo_id is null and f.status in ('auto', 'confirmed') "
            "order by f.person_id, f.status = 'confirmed' desc, f.w * f.h desc").fetchall()
    for r in rows:
        try:
            use_as_profile(r["person_id"], r["id"], auto=True)
        except Exception as e:  # a missing file shouldn't stop the rest
            print("avatar", r, e, flush=True)
    return len(rows)


# --- Corrections ----------------------------------------------------------------------------------

def assign(face_ids: list[int], person_id: int) -> None:
    """These faces are `person_id` (confirmed). A face tagged as someone else is also recorded as
    not them, so matching doesn't give it back. Only people have faces: a group is refused."""
    with db.connect() as conn, conn.transaction():
        if not conn.execute("select 1 from people where id = %s and kind = 'person'", (person_id,)).fetchone():
            raise ValueError("faces are tagged as people, not groups")
        conn.execute("insert into face_rejections (face_id, person_id) select id, person_id from faces "
                     "where id = any(%s) and person_id is not null and person_id <> %s on conflict do nothing",
                     (face_ids, person_id))
        conn.execute("delete from face_rejections where face_id = any(%s) and person_id = %s", (face_ids, person_id))
        conn.execute("update faces set person_id = %s, status = 'confirmed' where id = any(%s)",
                     (person_id, face_ids))


def reject(face_ids: list[int], person_id: int | None = None) -> None:
    """Not this person (by default, whoever they're tagged as now): the tags go and matching won't
    put them back; the faces can still be matched to someone else."""
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "insert into face_rejections (face_id, person_id) select id, coalesce(%s, person_id) from faces "
            "where id = any(%s) and coalesce(%s, person_id) is not null on conflict do nothing",
            (person_id, face_ids, person_id))
        conn.execute("update faces set status = null, person_id = null where id = any(%s) "
                     "and person_id = coalesce(%s, person_id) and status in ('auto', 'confirmed')",
                     (face_ids, person_id))
        # Profile photos picked automatically before they were tracked (unnamed): pick again.
        conn.execute("update people p set photo_id = null from media md where md.id = p.photo_id "
                     "and md.source = 'avatar' and md.name is null "
                     "and p.id in (select person_id from face_rejections where face_id = any(%s))", (face_ids,))


def move(face_ids: list[int], from_person: int, to_person: int) -> None:
    """These faces are someone else: not `from_person`, confirmed as `to_person`."""
    reject(face_ids, from_person)
    assign(face_ids, to_person)


def ignore(face_ids: list[int]) -> None:
    with db.connect() as conn:
        conn.execute("update faces set status = 'ignored', person_id = null where id = any(%s)", (face_ids,))


def confirm_person(person_id: int) -> None:
    with db.connect() as conn:
        conn.execute("update faces set status = 'confirmed' where person_id = %s and status = 'auto'", (person_id,))


UNKNOWN_GROUPS_SQL = """
select f.cluster, count(distinct f.media_id) as photos,
       array_agg(f.id order by f.w * f.h desc) as face_ids
from faces f
where f.status is null and f.cluster is not null
group by f.cluster having count(distinct f.media_id) >= 2
order by photos desc limit %s
"""


def unknown_groups(limit: int = 12) -> list[dict]:
    with db.connect() as conn:
        return conn.execute(UNKNOWN_GROUPS_SQL, (limit,)).fetchall()


def in_photo(media_id: int) -> list[dict]:
    """Faces in a photo, left to right, with who they are (rejected/ignored ones show as unknown)."""
    with db.connect() as conn:
        return conn.execute(
            "select f.id, f.x, f.y, f.w, f.h, f.status, "
            "case when f.status in ('auto', 'confirmed') then p.id end as person_id, "
            "case when f.status in ('auto', 'confirmed') then p.name end as name "
            "from faces f left join people p on p.id = f.person_id "
            "where f.media_id = %s and coalesce(f.status, '') <> 'ignored' order by f.x", (media_id,)).fetchall()


OF_PERSON_SQL = ("select f.id, f.status, f.media_id, f.cluster, m.sha256 from faces f join media m on m.id = f.media_id "
                 "where f.person_id = %s and f.status in ('auto', 'confirmed') "
                 "order by f.status = 'auto' desc, f.w * f.h desc")


def of_person(person_id: int) -> list[dict]:
    with db.connect() as conn:
        return conn.execute(OF_PERSON_SQL, (person_id,)).fetchall()


def in_photo_of(face_id: int) -> list[dict]:
    """`in_photo`'s row for one face (after a correction)."""
    with db.connect() as conn:
        m = conn.execute("select media_id from faces where id = %s", (face_id,)).fetchone()
    return [f for f in in_photo(m["media_id"]) if f["id"] == face_id] if m else []


# Faces that may be this person, from photos of entries where you were with them (@-tagged, in
# `People:` or picked; not just named, brought along by a group, or tagged from the photos
# themselves), and only faces with a reason:
#   - its group of similar faces already has faces tagged as them, or
#   - that group shows up in their photos far more than in photos overall (2+ photos, 2x as often);
#     one face (the clearest) stands for such a group, best first.
# Faces confirmed as someone else, and your own (in everything), are left out. For you: all photos,
# and the groups of faces in a tenth of them or more.
CANDIDATES_SQL = """
with me as (
    select (value ->> 'person_id')::bigint as id from settings where key = 'me'
), ph as (
    select distinct rm.media_id from raw_media rm
    where %(p)s = (select id from me)
       or rm.raw_id in (select raw_id from mentions where person_id = %(p)s and via_group is null
                                   and not from_photo and not named_only)
), cand as (
    select f.id, f.media_id, f.cluster, f.w * f.h as area,
           case when f.status in ('auto', 'confirmed') then f.person_id end as tagged_as
    from faces f
    where f.media_id in (select media_id from ph)
      and not (f.person_id is not distinct from %(p)s and f.status in ('auto', 'confirmed'))
      and coalesce(f.status, '') not in ('ignored', 'confirmed')
      and not (coalesce(f.status, '') = 'auto' and f.person_id is not distinct from (select id from me))
      and not exists (select 1 from face_rejections r where r.face_id = f.id and r.person_id = %(p)s)
), here as (
    select cluster, count(distinct media_id) as n from cand group by cluster
), everywhere as (
    select cluster, count(distinct media_id) as n from faces where cluster in (select cluster from here) group by cluster
), mine as (
    select distinct cluster from faces where person_id = %(p)s and status in ('auto', 'confirmed')
), totals as (
    select (select count(*) from ph) as ph, (select count(distinct media_id) from faces) as photos
)
select * from (
    select c.id, c.media_id, c.tagged_as, p.name as tagged_name, coalesce(h.n, 0) as together,
           c.cluster in (select cluster from mine) as same_group,
           coalesce(h.n * h.n::real / nullif(e.n, 0), 0) as score,
           row_number() over (partition by c.cluster order by c.tagged_as is null desc, c.area desc) as nth, c.area
    from cand c
    cross join totals t
    left join here h on h.cluster = c.cluster
    left join everywhere e on e.cluster = c.cluster
    left join people p on p.id = c.tagged_as
    where c.cluster in (select cluster from mine)
       or (%(p)s = (select id from me) and e.n * 10 >= t.photos)
       or (h.n >= 2 and h.n::real / t.ph >= 2 * e.n::real / nullif(t.photos, 0))
) x
where same_group or nth = 1  -- a look-alike group not known as them yet: its clearest face stands for it
order by same_group desc, tagged_as is null desc, score desc, area desc
limit %(limit)s
"""


def candidates(person_id: int, limit: int = 48) -> list[dict]:
    with db.connect() as conn:
        return conn.execute(CANDIDATES_SQL, {"p": person_id, "limit": limit}).fetchall()


def groups_of_person(person_id: int) -> list[list[dict]]:
    return group_by_look(of_person(person_id))


def group_by_look(found: list[dict]) -> list[list[dict]]:
    """Tagged faces by group of similar faces (biggest first): a wrong group is fixed at once."""
    by = collections.defaultdict(list)
    for f in found:
        by[f["cluster"]].append(f)
    return sorted(by.values(), key=len, reverse=True)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["scan"]:
        print(scan(limit=1000))
