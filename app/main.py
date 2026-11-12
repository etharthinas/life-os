"""Web app: FastAPI + Jinja + HTMX. Run with `uvicorn app.main:app`."""

import hashlib

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import unquote

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from PIL import UnidentifiedImageError
from psycopg.types.json import Jsonb

from . import agents, capture, config, contacts, days, db, faces, google, journal, media, people, storage
from .notetext import parse_birthday, split_frontmatter

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


def _local_time(ts, fmt="%m/%d %H:%M"):
    return ts.astimezone(config.TZ).strftime(fmt)


def _note_body(text: str, limit: int | None = None) -> str:
    body = media.plain(split_frontmatter(text)[1]).strip()
    return body if limit is None or len(body) <= limit else body[:limit].rstrip() + "…"


templates.env.filters["local"] = _local_time
templates.env.filters["note_body"] = _note_body
def _excerpt(text: str, q: str, width: int = 70) -> Markup:
    """The part of `text` around the first match of `q`, with the match highlighted."""
    i = text.lower().find(q.lower())
    if i < 0:
        return Markup(escape(text[:width * 2]))
    lo, hi = max(0, i - width), min(len(text), i + len(q) + width)
    return Markup("{}{}<mark>{}</mark>{}{}").format(
        "…" if lo else "", text[lo:i], text[i:i + len(q)], text[i + len(q):hi], "…" if hi < len(text) else "")


templates.env.filters["excerpt"] = _excerpt
templates.env.filters["md"] = lambda text: media.render_markdown(text, {}, {}, {})
templates.env.filters["linked"] = lambda text: media.render(text, {}, people.directory(), {})
templates.env.globals.update(initials=people.initials, hue=people.hue)

# Static files are fetched as /static/x?v=<content hash>: a new deploy changes the URL, so a phone
# never keeps an old stylesheet, and an unchanged file can be cached for good.
_STATIC_V = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()[:10] for f in (HERE / "static").iterdir()}
templates.env.globals["asset"] = lambda name: f"/static/{name}?v={_STATIC_V.get(name, '0')}"


@asynccontextmanager
async def lifespan(_app):
    db.migrate()
    yield


app = FastAPI(lifespan=lifespan)
# Pages go a long way (phone → server → back); gzip shrinks the HTML ~6x.
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    # iOS home-screen apps have no reload button; never let them hold on to an old page.
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/") and "v" in request.query_params:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


class FollowRedirects:
    """Saving a form answers "see /day/…" (303) and the browser then asks for that page: a second
    trip to the server. For htmx's requests the page comes straight back with the answer instead
    (HX-Push-Url puts its address in the bar, as following the redirect did)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers") or []) if scope["type"] == "http" else {}
        if scope["type"] != "http" or scope["method"] != "POST" or b"hx-request" not in headers:
            return await self.app(scope, receive, send)
        follow = None

        async def held(msg):
            nonlocal follow
            if msg["type"] == "http.response.start":
                loc = dict(msg.get("headers") or []).get(b"location", b"").decode()
                if msg["status"] in (302, 303) and loc.startswith("/") and not loc.startswith("//"):
                    follow = loc
                    return
            if follow is None:
                await send(msg)

        await self.app(scope, receive, held)
        if follow is None:
            return
        path, _, query = follow.partition("?")
        get = {**scope, "method": "GET", "path": unquote(path), "raw_path": path.encode(), "query_string": query.encode(),
               "headers": [(k, v) for k, v in scope["headers"] if k not in (b"content-type", b"content-length")]}

        async def nothing():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def answer(msg):
            if msg["type"] == "http.response.start" and headers.get(b"hx-boosted") == b"true":
                msg = {**msg, "headers": [*msg.get("headers", []), (b"hx-push-url", follow.encode())]}
            await send(msg)

        await self.app(get, nothing, answer)


app.add_middleware(FollowRedirects)


def _render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"today": capture.today(), **ctx})


def _parse_day(d: str | None) -> date:
    try:
        return date.fromisoformat(d) if d else capture.today()
    except ValueError:
        raise HTTPException(404)


def _todo_list(request: Request, d: str):
    """The todo list a change came from: a day page's (d set) or the Todos tab's."""
    if d:
        day = _parse_day(d)
        return _render(request, "_day_todos.html", day=day, day_todos=days.day_todos(day))
    return _render(request, "_todos.html", todos=capture.open_todos())


@app.get("/todos", response_class=HTMLResponse)
def todos_page(request: Request):
    return _render(request, "todos.html", todos=capture.open_todos())


@app.post("/todos", response_class=HTMLResponse)
def create_todo(request: Request, tasks: BackgroundTasks, text: str = Form(...), d: str = Form("")):
    """Scratchpad: the text is saved now and Claude sorts it into todos right after the response."""
    if not text.strip():
        return HTMLResponse("")
    cid = capture.scratch(text, source="web", default_due=_parse_day(d) if d else None)
    tasks.add_task(capture.process, cid)
    return _capture_card(request, cid)


@app.get("/todos/list", response_class=HTMLResponse)
def todo_list(request: Request, d: str = ""):
    return _todo_list(request, d)


def _capture_card(request: Request, cid: int):
    c = capture.load_capture(cid)
    if not c:
        raise HTTPException(404)
    age = (datetime.now(config.TZ) - c["created_at"]).total_seconds()
    resp = _render(request, "_capture.html", c=c, polling=c["status"] == "pending" and age < 180)
    if c["status"] != "pending":
        resp.headers["HX-Trigger"] = "todos-changed"  # the lists on the page re-fetch themselves
    return resp


@app.get("/captures/{cid}", response_class=HTMLResponse)
def capture_status(request: Request, cid: int):
    return _capture_card(request, cid)


@app.post("/captures/{cid}/undo", response_class=HTMLResponse)
def capture_undo(request: Request, cid: int):
    capture.undo_capture(cid)
    return _capture_card(request, cid)


@app.post("/todos/{todo_id}/status", response_class=HTMLResponse)
def update_todo(request: Request, todo_id: int, status: str = Form(...), d: str = Form("")):
    if status not in ("open", "done", "dropped"):
        raise HTTPException(400, "bad status")
    capture.set_todo_status(todo_id, status)
    return _todo_list(request, d)


@app.post("/logs")
def create_log(body: str = Form(""), d: str = Form(""), photos: list[UploadFile] = File([])):
    """Quick capture: a paragraph (and photos) appended to the day's journal."""
    day = _parse_day(d)
    media_ids = _ingest_uploads(photos)
    if body.strip() or media_ids:
        journal.append(body, source="web", d=day, media_ids=media_ids)
    return RedirectResponse("/" if day == capture.today() else f"/day/{day}", status_code=303)


@app.get("/capture", response_class=HTMLResponse)
def capture_page(request: Request, saved: str = ""):
    """A bare quick-capture box (the desktop app's ⌘⇧Space window)."""
    return _render(request, "capture.html", saved=saved)


@app.post("/capture")
def capture_submit(tasks: BackgroundTasks, text: str = Form(""), to: str = Form("journal")):
    if text.strip():
        if to == "todo":
            tasks.add_task(capture.process, capture.scratch(text, source="desktop"))
        else:
            journal.append(text, source="desktop")
    return RedirectResponse(f"/capture?saved={to}", status_code=303)


@app.post("/day/{d}/journal")
def save_day_journal(d: str, body: str = Form(""), photos: list[UploadFile] = File([])):
    day = _parse_day(d)
    journal.save_day(day, body, source="web", media_ids=_ingest_uploads(photos))
    return RedirectResponse("/" if day == capture.today() else f"/day/{day}", status_code=303)


@app.post("/day/{d}/journal/delete")
def delete_day_journal(d: str, back: str = Form("")):
    """Clear the day's log (text and photos). The people picked for the day stay."""
    day = _parse_day(d)
    journal.clear_day(day)
    if back.startswith("/"):
        return RedirectResponse(back, status_code=303)
    return RedirectResponse("/" if day == capture.today() else f"/day/{day}", status_code=303)


@app.get("/entries/{raw_id}/edit", response_class=HTMLResponse)
def edit_entry(request: Request, raw_id: int, back: str = ""):
    with db.connect() as conn:
        n = conn.execute("select * from raw_entries where id = %s", (raw_id,)).fetchone()
        if not n:
            raise HTTPException(404)
        text = journal.editable_text(conn, n)
    return _render(request, "_editor.html", action=f"/entries/{raw_id}", text=text, back=back or f"/notes/{raw_id}")


@app.get("/day/{d}/journal/edit", response_class=HTMLResponse)
def edit_day_journal(request: Request, d: str):
    day = _parse_day(d)
    with db.connect() as conn:
        doc = journal.find(conn, day)
        text = journal.editable_text(conn, doc) if doc else ""
    back = "/" if day == capture.today() else f"/day/{day}"
    return _render(request, "_editor.html", action=f"/day/{day}/journal", text=text, back=back,
                   inline=True, form_id="daylog-main")


@app.post("/entries/{raw_id}")
def save_entry(raw_id: int, body: str = Form(""), back: str = Form(""), photos: list[UploadFile] = File([])):
    try:
        journal.save(raw_id, body, _ingest_uploads(photos))
    except KeyError:
        raise HTTPException(404)
    return RedirectResponse(back if back.startswith("/") else f"/notes/{raw_id}", status_code=303)


# --- Day view & planning ------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
@app.get("/day", response_class=HTMLResponse)
@app.get("/day/{d}", response_class=HTMLResponse)
def day_view(request: Request, d: str | None = None):
    day = _parse_day(d)
    data = days.load_day(day)
    people.render_entries(data["entries"])
    timed = [{"type": "event", "start": e["start_at"], **e} for e in data["events"] if not e["all_day"]]
    timed += [{"type": "block", "start": b["start_at"], **b} for b in data["blocks"]]
    timed.sort(key=lambda x: x["start"])
    sunday = day - timedelta(days=(day.weekday() + 1) % 7)
    week = [(sunday + timedelta(days=i), data["week"].get(sunday + timedelta(days=i), 0)) for i in range(7)]
    return _render(request, "day.html", day=day, prev=day - timedelta(days=1), next=day + timedelta(days=1),
                   all_day=[e for e in data["events"] if e["all_day"]], timed=timed, week=week,
                   blocks=data["blocks"], day_todos=data["day_todos"], day_people=data["meetings"],
                   doc=next((e for e in data["entries"] if e["meta"].get("type") == "daily"), None),
                   connected=bool(data["calendars"]), settings=data["settings"])


@app.get("/journal", response_class=HTMLResponse)
def journal_timeline(request: Request, before: str = ""):
    """Every day journal, newest first, as one scroll; more load as you reach the end."""
    try:
        cursor = datetime.fromisoformat(before) if before else None
    except ValueError:
        raise HTTPException(400)
    entries, nxt = journal.timeline(cursor)
    people.render_entries(entries)
    entries = [e for e in entries if e["html"] or e["extra_photos"]]
    page = "_journal_page.html" if request.headers.get("HX-Request") and before else "journal.html"
    return _render(request, page, entries=entries, next=nxt.isoformat() if nxt else None)


@app.get("/calendar", response_class=HTMLResponse)
def month_view(request: Request, m: str = ""):
    """A month at a glance (like Notion Calendar): events, todos due, birthdays, days with a log."""
    today = capture.today()
    try:
        year, month = (int(x) for x in m.split("-")) if m else (today.year, today.month)
        first = date(year, month, 1)
    except ValueError:
        raise HTTPException(404)
    prev = (first - timedelta(days=1)).replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return _render(request, "calendar.html", first=first, prev=prev, next=nxt, **days.load_month(year, month))


@app.post("/day/{d}/plan")
def plan_day(d: str):
    days.make_plan(_parse_day(d))
    return RedirectResponse(f"/day/{d}", status_code=303)


@app.post("/day/{d}/accept")
def accept_blocks(d: str, block_id: list[int] = Form(...)):
    days.accept(block_id)
    return RedirectResponse(f"/day/{d}", status_code=303)


@app.post("/blocks/{block_id}/dismiss")
def dismiss_block(block_id: int, d: str = Form(...)):
    days.dismiss(block_id)
    return RedirectResponse(f"/day/{d}", status_code=303)


# --- People ------------------------------------------------------------------------------------

@app.get("/people", response_class=HTMLResponse)
def people_list(request: Request, q: str = "", kind: str = "", org: str = "", show: str = "", sort: str = "recent"):
    if kind not in ("", "person", "group"):
        kind = ""
    org_id = int(org) if org.isdigit() else None
    show = show if show in people.SHOWS else ""
    sort = sort if sort in people.SORTS else "recent"
    data = people.list_people(q.strip(), kind, org_id, show, sort)
    filtered = bool(q.strip() or kind or org_id or show or sort != "recent")
    return _render(request, "people.html", q=q, kind=kind, org=org_id, show=show, sort=sort,
                   browsing=not filtered, **data)


@app.post("/people")
def create_person(name: str = Form(...), kind: str = Form("person"), about: str = Form("")):
    row = people.create(name, kind, about)
    return RedirectResponse(f"/people/{row['id']}", status_code=303)


@app.get("/people/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, matched: int | None = None, added: int | None = None):
    return _render(request, "contacts.html", pending=contacts.pending(), matched=matched, added=added)


@app.post("/people/contacts/import")
def contacts_import(file: UploadFile = File(...)):
    raw = file.file.read()
    text = raw.decode("utf-8-sig", "replace")
    result = contacts.import_cards(contacts.parse(text))
    people.forget_directory()
    return RedirectResponse(f"/people/contacts?matched={result['matched']}", status_code=303)


@app.post("/people/contacts/add")
def contacts_add(uids: list[str] = Form([])):
    n = contacts.add_pending(uids)
    people.forget_directory()
    return RedirectResponse(f"/people/contacts?added={n}", status_code=303)


@app.post("/people/contacts/dismiss")
def contacts_dismiss():
    contacts.dismiss_pending()
    return RedirectResponse("/people/contacts", status_code=303)


@app.get("/people/contacts.vcf")
def contacts_export():
    return Response(contacts.all_vcards(), media_type="text/vcard; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="life-os-people.vcf"'})


@app.get("/people/{person_id}/contact.vcf")
def person_vcard(person_id: int):
    with db.connect() as conn:
        p = conn.execute("select * from people where id = %s", (person_id,)).fetchone()
    if not p:
        raise HTTPException(404)
    # inline: a phone opens its "Add to Contacts" screen
    return Response(contacts.vcard(p, [g["name"] for g in people.person_orgs(person_id)]),
                    media_type="text/vcard; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="contact-{person_id}.vcf"'})


@app.get("/people/mention.json")
def people_mention(q: str = ""):
    """For @-autocomplete while writing."""
    return [{"id": p["id"], "name": p["name"], "kind": p["kind"], "photo": p["photo"],
             "sub": p["about"] or ("Group" if p["kind"] == "group" else "")}
            for p in people.suggest(q.strip(), not_me=True)]


@app.get("/people/graph", response_class=HTMLResponse)
def people_graph(request: Request):
    return _render(request, "graph.html", orgs=people.list_people(kind="group")["orgs"][:10])


@app.get("/people/graph.json")
def people_graph_data():
    return people.graph()


@app.get("/people/suggest", response_class=HTMLResponse)
def people_suggest(request: Request, q: str = "", to: str = ""):
    """The people picker's dropdown: matches (or recent people), and an option to add a new one.

    `to` says what picking does: "day:2026-10-26" (who was there), "face:12" (who this face is),
    "group:7" (who this group of faces is; the face ids come from the picker's form).
    """
    kind, _, arg = to.partition(":")
    taken, person_only = [], False
    if kind == "day":  # "day:2026-10-26" or "day:2026-10-26:1" (the day's second meeting)
        d, _, n = arg.partition(":")
        day, meeting = _parse_day(d), int(n) if n.isdigit() else 0
        mts = journal.day_people(day)
        taken = [x["id"] for x in journal.everyone([mt for mt in mts if mt["n"] == meeting])]
        action, target, include = f"/day/{day}/people?meeting={meeting}", "#day-people", ""
    elif kind == "face" and arg.isdigit():  # faces are people, never groups
        person_only = True
        action, target, include = f"/faces/{arg}/assign", f"#face-{arg}", ""
    elif kind == "group" and arg.isdigit():
        person_only = True
        action, target, include = f"/faces/group/{arg}/assign", f"#group-{arg}", f"#group-{arg} [name=face_ids]"
    elif kind == "org" and arg.isdigit():  # add an organization to a person
        taken = [int(arg)] + [g["id"] for g in people.person_orgs(int(arg))]
        action, target, include = f"/people/{arg}/orgs", "#orgs", ""
        return _render(request, "_people_suggest.html", q=q.strip(), action=action, target=target, include=include,
                       found=people.suggest(q.strip(), exclude=taken, kind="group"),
                       exists=False, new_label="New group")
    elif kind == "member" and arg.isdigit():  # add a member to a group
        with db.connect() as conn:
            taken = [r["person_id"] for r in conn.execute("select person_id from memberships where group_id = %s",
                                                          (int(arg),))]
        return _render(request, "_people_suggest.html", q=q.strip(), action=f"/people/{arg}/members",
                       target="#members", include="", found=people.suggest(q.strip(), exclude=taken, kind="person"),
                       exists=q.strip() in people.directory())
    elif kind == "move" and arg.isdigit():  # selected faces on a person's face page are someone else
        person_only = True
        taken = [int(arg)]
        action, target, include = f"/people/{arg}/faces/move", "#face-groups", "#face-groups [name=face_ids]"
    else:
        raise HTTPException(400)
    return _render(request, "_people_suggest.html", q=q.strip(), action=action, target=target, include=include,
                   found=people.suggest(q.strip(), exclude=taken, kind="person" if person_only else "",
                                        not_me=kind == "day"),
                   exists=q.strip() in people.directory())


@app.post("/day/{d}/people", response_class=HTMLResponse)
def day_pick_person(request: Request, d: str, person_id: int | None = Form(None), name: str = Form(""),
                    meeting: int = 0):
    day = _parse_day(d)
    if person_id is None:
        person_id = people.create(name)["id"]
    journal.pick_person(day, person_id, meeting)
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day))


# Photos of the day's journal not scanned for faces yet.
UNSCANNED_SQL = """
select count(*) as n from raw_media rm join media m on m.id = rm.media_id
join raw_entries r on r.id = rm.raw_id and r.meta ->> 'type' = 'daily'
where r.occurred_at >= %s and r.occurred_at < %s and not m.faces_scanned
"""


@app.post("/day/{d}/faces/tag", response_class=HTMLResponse)
def day_tag_faces(request: Request, d: str):
    """"Tag people from photos": the worker finds the faces now; the card waits for it."""
    day = _parse_day(d)
    faces.request_scan()
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day), scanning=True)


@app.get("/day/{d}/faces/status", response_class=HTMLResponse)
def day_tag_faces_status(request: Request, d: str, n: int = 0):
    """Polled every 2 s while faces are being found (for up to 2 minutes): the card, with who's
    tagged once the worker is done."""
    day = _parse_day(d)
    with db.connect() as conn:
        left = conn.execute(UNSCANNED_SQL, journal._bounds(day)).fetchone()["n"]
    busy = faces.scan_requested()
    if left and not busy:
        faces.request_scan()  # asked for while the worker was finishing another round: ask again
    waiting = bool(left or busy) and faces.available() and n < 60
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day),
                   scanning=waiting, poll=n + 1, scan_done="tagged" if not (left or busy) else "timeout" if not waiting else "")


# Groups that have all of these people in them, the tightest first (묶기 suggestions).
LABEL_OPTIONS_SQL = """
select g.id, g.name, count(*) over () as n_found,
       (select count(*) from memberships x where x.group_id = g.id) as size
from people g
where g.kind = 'group'
  and (select count(*) from memberships ms where ms.group_id = g.id and ms.person_id = any(%(ids)s)) = cardinality(%(ids)s)
order by size, g.name
limit 8
"""


@app.get("/day/{d}/label/options", response_class=HTMLResponse)
def day_label_options(request: Request, d: str, ids: str = "", meeting: int = 0, gids: str = ""):
    """묶기: the groups the selected people are all in, and making a new group of them."""
    day = _parse_day(d)
    pids = [int(x) for x in ids.split(",") if x.isdigit()]
    if not pids:
        return HTMLResponse("")
    with db.connect() as conn:
        found = conn.execute(LABEL_OPTIONS_SQL, {"ids": pids}).fetchall()
    return _render(request, "_label_options.html", day=day, found=found, ids=pids, meeting=meeting,
                   gids=[int(x) for x in gids.split(",") if x.isdigit()])


@app.post("/day/{d}/label", response_class=HTMLResponse)
def day_label(request: Request, d: str, meeting: int = Form(0), group_id: int | None = Form(None),
              name: str = Form(""), ids: str = Form(""), gids: str = Form("")):
    """묶기 → a group: label the meeting's people with it. A new name makes the group, with them in it."""
    day = _parse_day(d)
    pids = [int(x) for x in ids.split(",") if x.isdigit()]
    if group_id is None:
        if not name.strip():
            raise HTTPException(400)
        group_id = people.create(name, kind="group")["id"]
        for pid in pids:
            people.join_org(pid, group_id)
    journal.label_group(day, meeting, group_id, pids, [int(x) for x in gids.split(",") if x.isdigit()])
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day))


@app.post("/day/{d}/meetings", response_class=HTMLResponse)
def day_add_meeting(request: Request, d: str):
    day = _parse_day(d)
    journal.add_meeting(day)
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day))


@app.post("/day/{d}/meetings/order", response_class=HTMLResponse)
def day_reorder_meetings(request: Request, d: str, order: str = Form("")):
    day = _parse_day(d)
    journal.reorder_meetings(day, [int(x) for x in order.split(",") if x.isdigit()])
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day))


@app.post("/day/{d}/move", response_class=HTMLResponse)
def day_move_people(request: Request, d: str, ids: str = Form(""), meeting: int = Form(0)):
    """A chip dragged to another meeting (a group's chip carries the people under it)."""
    day = _parse_day(d)
    journal.move_to_meeting(day, [int(x) for x in ids.split(",") if x.isdigit()], meeting)
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day))


@app.post("/day/{d}/groups/{group_id}/ungroup", response_class=HTMLResponse)
def day_ungroup(request: Request, d: str, group_id: int, ids: str = Form("")):
    """전체 해제 (long press / right-click on a group): the label goes, its people stay, ungrouped."""
    day = _parse_day(d)
    journal.ungroup(day, group_id, [int(x) for x in ids.split(",") if x.isdigit()])
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day))


@app.post("/day/{d}/meetings/{n}", response_class=HTMLResponse)
def day_rename_meeting(d: str, n: int, title: str = Form("")):
    journal.rename_meeting(_parse_day(d), n, title)
    return HTMLResponse("")


@app.post("/day/{d}/meetings/{n}/remove", response_class=HTMLResponse)
def day_remove_meeting(request: Request, d: str, n: int):
    day = _parse_day(d)
    journal.remove_meeting(day, n)
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day))


@app.post("/day/{d}/people/{person_id}/remove", response_class=HTMLResponse)
def day_unpick_person(request: Request, d: str, person_id: int):
    day = _parse_day(d)
    journal.unpick_person(day, person_id)
    return _render(request, "_day_people.html", day=day, day_people=journal.day_people(day))


@app.post("/entries/{raw_id}/name")
def entry_choose_name(raw_id: int, name: str = Form(...), person_id: int | None = Form(None)):
    """"Who is 민준?" → this person (for this entry). The page reloads to show it everywhere."""
    try:
        journal.choose_name(raw_id, name, person_id)
    except KeyError:
        raise HTTPException(404)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/entries/{raw_id}/link")
def entry_link(raw_id: int, text: str = Form(...), person_id: int = Form(...)):
    """Select a word → Link to someone: everywhere it's written in this entry."""
    text = text.strip()
    if not text or len(text) > 40:
        raise HTTPException(400)
    try:
        journal.link_text(raw_id, text, person_id)
    except KeyError:
        raise HTTPException(404)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/entries/{raw_id}/unlink")
def entry_unlink(raw_id: int, text: str = Form(...)):
    """"Not a name here" (or undo a link you made)."""
    try:
        journal.unlink_text(raw_id, text)
    except KeyError:
        raise HTTPException(404)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/entries/{raw_id}/never")
def entry_never(raw_id: int, background: BackgroundTasks, text: str = Form(...)):
    """"Never a name": this entry at once, every other entry that has the word in the background."""
    people.add_never_name(text)
    try:
        journal.unlink_text(raw_id, text)
    except KeyError:
        raise HTTPException(404)
    background.add_task(journal.resync_containing, text)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


AMBIGUOUS_SQL = """
select id, body, occurred_at, meta, kind, source from raw_entries
where jsonb_array_length(coalesce(meta -> 'ambiguous', '[]')) > 0
   or exists (select 1 from jsonb_each(coalesce(meta -> 'names', '{}')) n where n.value ->> 'how' = 'guess')
order by occurred_at desc
"""


@app.get("/people/ambiguous", response_class=HTMLResponse)
def people_ambiguous(request: Request):
    """Entries with a name several people share ("민준"): the ones nobody could tell, then the guesses."""
    with db.connect() as conn:
        entries = conn.execute(AMBIGUOUS_SQL).fetchall()
    people.render_entries(entries)
    open_ = [e for e in entries if e["meta"].get("ambiguous")]
    guessed = [e for e in entries if not e["meta"].get("ambiguous")]
    return _render(request, "ambiguous.html", open_=open_, guessed=guessed)


@app.get("/people/{person_id}", response_class=HTMLResponse)
def person(request: Request, person_id: int, saved: str = ""):
    data = people.load_person(person_id, limit=None)  # 10 shown, the rest behind "Show older"
    if not data:
        raise HTTPException(404)
    p = data["p"]
    me = data["me"] or {}
    with db.connect() as conn:
        options = people.detail_options(conn)
    if p["kind"] == "group":
        data["group"] = people.group_page(person_id)
        data["all_groups"] = [g for g in people.list_people(kind="group")["people"] if g["id"] != person_id]
    return _render(request, "person.html", options=options, saved=saved, is_me=me.get("person_id") == person_id,
                   phones=contacts.phones_of(p), emails=(p["contact"] or {}).get("emails") or [], **data)


def _members(request: Request, group_id: int):
    with db.connect() as conn:
        g = conn.execute("select id, name from people where id = %s", (group_id,)).fetchone()
        members = conn.execute(people.MEMBERS_SQL, {"id": group_id}).fetchall()
    return _render(request, "_members.html", g=g, members=members)


@app.post("/people/{group_id}/members", response_class=HTMLResponse)
def group_add_member(request: Request, group_id: int, person_id: int | None = Form(None), name: str = Form("")):
    if person_id is None:
        person_id = people.create(name)["id"]
    people.join_org(person_id, group_id)
    return _members(request, group_id)


@app.post("/people/{group_id}/members/{person_id}/remove", response_class=HTMLResponse)
def group_remove_member(request: Request, group_id: int, person_id: int):
    people.leave_org(person_id, group_id)
    return _members(request, group_id)


@app.post("/people/{group_id}/subgroups")
def group_add_subgroup(group_id: int, name: str = Form(...)):
    g = people.create_subgroup(group_id, name)
    return RedirectResponse(f"/people/{g['id']}", status_code=303)


@app.post("/people/{group_id}/parent")
def group_set_parent(group_id: int, parent_id: str = Form("")):
    try:
        people.set_parent(group_id, int(parent_id) if parent_id.isdigit() else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    people.forget_directory()
    return RedirectResponse(f"/people/{group_id}", status_code=303)


@app.post("/people/{person_id}/orgs", response_class=HTMLResponse)
def person_org_add(request: Request, person_id: int, group_id: int | None = Form(None, alias="person_id"),
                   name: str = Form("")):
    if group_id is None:
        group_id = people.create(name, kind="group")["id"]
    people.join_org(person_id, group_id)
    return _render(request, "_orgs.html", pid=person_id, orgs=people.person_orgs(person_id))


@app.post("/people/{person_id}/orgs/{group_id}/remove", response_class=HTMLResponse)
def person_org_remove(request: Request, person_id: int, group_id: int):
    people.leave_org(person_id, group_id)
    return _render(request, "_orgs.html", pid=person_id, orgs=people.person_orgs(person_id))


@app.post("/people/{person_id}/birthday-reminders")
def birthday_reminders(person_id: int, hide: int = Form(1), back: str = Form("")):
    with db.connect() as conn:
        conn.execute("update people set hide_birthday = %s where id = %s", (bool(hide), person_id))
    return RedirectResponse(back if back.startswith("/") else f"/people/{person_id}", status_code=303)


@app.post("/people/{person_id}")
def edit_person(person_id: int, name: str = Form(""), about: str = Form(""), aliases: str = Form(""),
                birthday: str = Form(""), keep_in_touch_days: str = Form(""), phones: str | None = Form(None),
                emails: str | None = Form(None), school: str | None = Form(None), majors: list[str] = Form([]),
                class_of: str | None = Form(None), details: str | None = Form(None)):
    y, mo, d = parse_birthday(birthday)
    if birthday.strip() and mo is None:
        raise HTTPException(400, "birthday: use YYYY-MM-DD or MM-DD")
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    with db.connect() as conn:
        old = conn.execute("select name, contact from people where id = %s", (person_id,)).fetchone()
        if not old:
            raise HTTPException(404)
        name = people.nfc(name.strip()) or old["name"]
        if name != old["name"] and old["name"] not in alias_list:
            alias_list.append(old["name"])  # so [[old name]] links written before still find them
        conn.execute("update people set name = %s, about = %s, aliases = %s, birth_year = %s, birth_month = %s, "
                     "birth_day = %s, keep_in_touch_days = %s where id = %s",
                     (name, about.strip() or None, alias_list, y, mo, d,
                      int(keep_in_touch_days) if keep_in_touch_days.isdigit() and int(keep_in_touch_days) else None,
                      person_id))
        # The standard details, written the way they already are elsewhere ("고려대학교" → 고려대).
        # (`details` marks the person form: with every major taken off, no `majors` come at all.)
        if details is not None:
            known = people.detail_options(conn)
            yy = int(class_of) % 100 if (class_of or "").strip().isdigit() else None
            majors_std = [m for m in (people.standard_detail(x, known["majors"]) for x in majors) if m]
            conn.execute("update people set school = %s, majors = %s, class_of = %s where id = %s",
                         (people.standard_detail(school or "", known["schools"], people.SCHOOL_NAMES),
                          list(dict.fromkeys(majors_std)), yy, person_id))
        # Phone numbers and emails live with the contact card.
        contact = dict(old["contact"] or {})
        if phones is not None:
            contact["phones"] = [contacts.format_phone(x) for x in phones.split(",") if x.strip()]
        if emails is not None:
            contact["emails"] = [x.strip() for x in emails.split(",") if x.strip()]
        conn.execute("update people set contact = %s where id = %s", (Jsonb(contact), person_id))
    people.forget_directory()
    return RedirectResponse(f"/people/{person_id}?saved=1", status_code=303)


def _ingest_uploads(files: list[UploadFile]) -> list[int]:
    ids = []
    for f in files:
        if f.filename:
            if not f.size:  # the phone sent the file without its bytes: say so instead of saving nothing
                raise HTTPException(400, f"photo arrived empty: {f.filename}")
            try:
                ids.append(media.ingest(f.file.read(), None, "web"))  # named by hash when embedded
            except UnidentifiedImageError:
                raise HTTPException(400, f"not an image: {f.filename}")
    if ids:
        faces.request_scan()  # who's in them: the worker looks within seconds
    return ids


@app.post("/people/{person_id}/log")
def person_log(person_id: int, body: str = Form(""), photos: list[UploadFile] = File([])):
    """Goes into today's journal, with the person picked for the day so it shows on their page."""
    media_ids = _ingest_uploads(photos)
    if body.strip() or media_ids:
        journal.append(body, source="web", media_ids=media_ids)
        journal.pick_person(capture.today(), person_id)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@app.post("/people/{person_id}/gifts", response_class=HTMLResponse)
def gifts(request: Request, person_id: int, add: str = Form(""), remove: str = Form("")):
    with db.connect() as conn:
        if add.strip():
            conn.execute("update people set gift_ideas = array_append(array_remove(gift_ideas, %s), %s) "
                         "where id = %s", (add.strip(), add.strip(), person_id))
        if remove:
            conn.execute("update people set gift_ideas = array_remove(gift_ideas, %s) where id = %s",
                         (remove, person_id))
        p = conn.execute("select id, gift_ideas from people where id = %s", (person_id,)).fetchone()
    return _render(request, "_gifts.html", p=p)


@app.post("/people/{person_id}/photo")
def set_photo(person_id: int, media_id: str = Form(""), photo: UploadFile | None = File(None)):
    media_id = int(media_id) if media_id.isdigit() else None  # empty = remove the photo
    if photo is not None and photo.filename:
        media_id = (_ingest_uploads([photo]) or [None])[0]
    with db.connect() as conn:
        conn.execute("update people set photo_id = %s where id = %s", (media_id, person_id))
    return RedirectResponse(f"/people/{person_id}", status_code=303)


def _photo_context(conn, media_id: int) -> dict:
    """What photo.html shows under any photo: the entries it's from and who is in them."""
    with conn.pipeline():
        # Who else is in the notes this photo comes from: candidates for "use as their photo".
        q_in = conn.execute(
            "select distinct p.id, p.name, p.photo_id, ph.sha256 as photo from raw_media rm "
            "join mentions m on m.raw_id = rm.raw_id join people p on p.id = m.person_id "
            "left join media ph on ph.id = p.photo_id "
            "where rm.media_id = %s and p.kind = 'person' order by p.name", (media_id,))
        q_notes = conn.execute(
            "select r.id, r.occurred_at, r.meta ->> 'title' as title from raw_media rm "
            "join raw_entries r on r.id = rm.raw_id where rm.media_id = %s order by r.occurred_at", (media_id,))
    return {"people_in": q_in.fetchall(), "notes": q_notes.fetchall(), "faces": faces.in_photo(media_id)}


@app.get("/people/{person_id}/photos/{media_id}", response_class=HTMLResponse)
def person_photo(request: Request, person_id: int, media_id: int):
    photos = people.gallery(person_id)
    i = next((i for i, ph in enumerate(photos) if ph["id"] == media_id), None)
    if i is None:
        raise HTTPException(404)
    with db.connect() as conn:
        p = conn.execute("select id, name, photo_id from people where id = %s", (person_id,)).fetchone()
        return _render(request, "photo.html", p=p, ph=photos[i], n=len(photos), i=i,
                       prev=photos[i - 1] if i else None, next=photos[i + 1] if i + 1 < len(photos) else None,
                       **_photo_context(conn, media_id))


@app.get("/photos/{sha}", response_class=HTMLResponse)
def photo(request: Request, sha: str):
    """Any photo (from a journal or a note), with its faces; back goes wherever you came from."""
    with db.connect() as conn:
        ph = conn.execute(
            "select m.id, m.sha256, m.taken_at, min(r.occurred_at) as occurred_at from media m "
            "left join raw_media rm on rm.media_id = m.id left join raw_entries r on r.id = rm.raw_id "
            "where m.sha256 = %s group by m.id", (sha,)).fetchone()
        if not ph:
            raise HTTPException(404)
        return _render(request, "photo.html", p=None, ph=ph, **_photo_context(conn, ph["id"]))


# --- Faces ------------------------------------------------------------------------------------------

@app.get("/faces/{face_id}.webp")
def face_image(face_id: int):
    path = config.MEDIA_CACHE_DIR / "faces" / f"{face_id}.webp"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        with db.connect() as conn:
            f = conn.execute(faces.FACE_SQL, (face_id,)).fetchone()
        if not f:
            raise HTTPException(404)
        try:
            data = faces.crop(f)
        except KeyError:
            raise HTTPException(404)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    # A face's box never changes, so neither do its bytes.
    return Response(data, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/faces", response_class=HTMLResponse)
def faces_review(request: Request):
    """Who is this? Groups of similar faces nobody is tagged in yet, biggest first."""
    return _render(request, "faces.html", groups=faces.unknown_groups())


def _face_row(request: Request, face_id: int):
    f = next((f for f in faces.in_photo_of(face_id)), None)
    return _render(request, "_face_row.html", f=f) if f else HTMLResponse("")


@app.post("/faces/{face_id}/assign", response_class=HTMLResponse)
def face_assign(request: Request, face_id: int,
                person_id: int | None = Form(None), name: str = Form("")):
    if person_id is None:
        person_id = people.create(name)["id"]
    faces.assign([face_id], person_id)
    faces.request_relabel()
    return _face_row(request, face_id)


@app.post("/faces/{face_id}/reject", response_class=HTMLResponse)
def face_reject(request: Request, face_id: int):
    faces.reject([face_id])
    faces.request_relabel()
    return _face_row(request, face_id)


@app.post("/faces/group/{cluster}/assign", response_class=HTMLResponse)
def group_assign(cluster: int, face_ids: list[int] = Form(...),
                 person_id: int | None = Form(None), name: str = Form("")):
    if person_id is None:
        person_id = people.create(name)["id"]
    faces.assign(face_ids, person_id)
    faces.request_relabel()
    with db.connect() as conn:
        who = conn.execute("select name from people where id = %s", (person_id,)).fetchone()["name"]
    return HTMLResponse(Markup('<p class="group-done">Tagged as <a href="/people/{0}">{1}</a> in {2} photos.</p>')
                        .format(person_id, who, len(face_ids)))


@app.post("/faces/group/{cluster}/ignore", response_class=HTMLResponse)
def group_ignore(cluster: int, face_ids: list[int] = Form(...)):
    faces.ignore(face_ids)
    return HTMLResponse("")


@app.get("/people/{person_id}/faces", response_class=HTMLResponse)
def person_faces(request: Request, person_id: int):
    with db.connect() as conn:
        with conn.pipeline():  # one round trip
            q_p = conn.execute("select p.id, p.name, p.kind, ph.sha256 as photo from people p left join media ph "
                               "on ph.id = p.photo_id where p.id = %s", (person_id,))
            q_faces = conn.execute(faces.OF_PERSON_SQL, (person_id,))
            q_cands = conn.execute(faces.CANDIDATES_SQL, {"p": person_id, "limit": 48})
            q_me = conn.execute("select value from settings where key = 'me'")
        p = q_p.fetchone()
        if not p:
            raise HTTPException(404)
        if p["kind"] == "group":  # a group has no face of its own
            return RedirectResponse(f"/people/{person_id}", status_code=303)
        mine, cands, me = q_faces.fetchall(), q_cands.fetchall(), q_me.fetchone()
    return _render(request, "person_faces.html", p=p, groups=faces.group_by_look(mine), candidates=cands,
                   is_me=bool(me) and (me["value"] or {}).get("person_id") == person_id)


@app.post("/people/{person_id}/delete")
def delete_person(person_id: int):
    """Remove a person (or group): their mentions, memberships and face tags go with them; the
    journal text and photos stay."""
    with db.connect() as conn, conn.transaction():
        conn.execute("delete from settings where key = 'me' and (value ->> 'person_id')::bigint = %s", (person_id,))
        conn.execute("update faces set status = null where person_id = %s", (person_id,))
        if not conn.execute("delete from people where id = %s returning id", (person_id,)).fetchone():
            raise HTTPException(404)
    people.forget_directory()
    faces.request_relabel()
    people.request_regroup()
    return RedirectResponse("/people", status_code=303)


@app.post("/people/{person_id}/me")
def person_is_me(person_id: int, on: str = Form("1")):
    """"This is me": the face in most of the photos is tagged as this person."""
    faces.set_me(person_id if on == "1" else None)
    return RedirectResponse(f"/people/{person_id}/faces", status_code=303)


@app.post("/people/{person_id}/faces/claim", response_class=HTMLResponse)
def person_faces_claim(person_id: int, face_id: int = Form(...)):
    """"Is this them?" → yes. Returns nothing: the face leaves the suggestions."""
    faces.assign([face_id], person_id)
    faces.request_relabel()
    return HTMLResponse("")


def _face_groups(request: Request, person_id: int):
    with db.connect() as conn:
        p = conn.execute("select id, name from people where id = %s", (person_id,)).fetchone()
    return _render(request, "_face_groups.html", p=p, groups=faces.groups_of_person(person_id))


@app.post("/people/{person_id}/faces/reject", response_class=HTMLResponse)
def person_faces_reject(request: Request, person_id: int, face_ids: list[int] = Form(...)):
    faces.reject(face_ids, person_id)
    faces.request_relabel()
    return _face_groups(request, person_id)


@app.post("/people/{person_id}/faces/move", response_class=HTMLResponse)
def person_faces_move(request: Request, person_id: int, face_ids: list[int] = Form(...),
                      to_id: int | None = Form(None, alias="person_id"), name: str = Form("")):
    if to_id is None:
        to_id = people.create(name)["id"]
    faces.move(face_ids, person_id, to_id)
    faces.request_relabel()
    return _face_groups(request, person_id)


@app.post("/people/{person_id}/faces/confirm")
def person_faces_confirm(person_id: int):
    faces.confirm_person(person_id)
    return RedirectResponse(f"/people/{person_id}/faces", status_code=303)


@app.post("/people/{person_id}/photo/face")
def person_photo_from_face(person_id: int, face_id: int = Form(...)):
    try:
        faces.use_as_profile(person_id, face_id)
    except KeyError:
        raise HTTPException(404)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@app.get("/sw.js")
def service_worker():
    # At the root so it covers every page; never cached, so a new version is picked up at once.
    return Response((HERE / "static" / "sw.js").read_bytes(), media_type="text/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/media/{sha}/{variant}")
def media_file(sha: str, variant: str):
    if not media.SHA.fullmatch(sha) or variant not in ("orig", "display", "thumb"):
        raise HTTPException(404)
    content_type = "image/webp"
    if variant == "orig":
        with db.connect() as conn:
            row = conn.execute("select content_type from media where sha256 = %s", (sha,)).fetchone()
        if not row:
            raise HTTPException(404)
        content_type = row["content_type"]
    try:
        data = storage.store().get(media.key(sha, variant))
    except KeyError:
        raise HTTPException(404)
    # Keyed by content hash, so a URL's bytes never change.
    return Response(data, media_type=content_type,
                    headers={"Cache-Control": "private, max-age=31536000, immutable"})


# --- Notes, search ------------------------------------------------------------------------------

@app.get("/notes/{raw_id}", response_class=HTMLResponse)
def note(request: Request, raw_id: int):
    with db.connect() as conn:
        n = conn.execute("select * from raw_entries where id = %s", (raw_id,)).fetchone()
    if not n:
        raise HTTPException(404)
    people.render_entries([n])
    return _render(request, "note.html", n=n)


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    q = q.strip()
    rows, found = [], []
    if len(q) >= 2:
        with db.connect() as conn:
            with conn.pipeline():
                q_notes = conn.execute(
                    "select id, body, occurred_at, meta from raw_entries where body ilike '%%' || %s || '%%' "
                    "order by occurred_at desc limit 50", (q,))
                q_people = conn.execute(
                    "select p.id, p.name, ph.sha256 as photo from people p left join media ph on ph.id = p.photo_id "
                    "where p.name ilike '%%' || %s || '%%' or array_to_string(p.aliases, ' ') ilike '%%' || %s || '%%' "
                    "order by p.name limit 20", (q, q))
            rows, found = q_notes.fetchall(), q_people.fetchall()
    return _render(request, "search.html", q=q, results=rows, found_people=found)


# --- Settings -----------------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, synced: str = ""):
    with db.connect() as conn:
        with conn.pipeline():
            q_s = conn.execute("select key, value from settings")
            q_c = conn.execute("select * from calendars order by is_primary desc, summary")
            q_a = conn.execute("select email from google_accounts")
        s = {r["key"]: r["value"] for r in q_s.fetchall()}
        cals = q_c.fetchall()
        target = s.get("block_calendar_id") or next((c["id"] for c in cals if c["is_primary"]), None)
        return _render(request, "settings.html", s=s, calendars=cals, target=target,
                       accounts=[a["email"] for a in q_a.fetchall()], synced=synced)


@app.post("/settings")
def save_settings(day_start: str = Form(...), day_end: str = Form(...), default_estimate_min: int = Form(...),
                  block_calendar_id: str = Form(""), synced_calendar: list[str] = Form([])):
    days.set_setting("day_start", day_start)
    days.set_setting("day_end", day_end)
    days.set_setting("default_estimate_min", default_estimate_min)
    if block_calendar_id:
        days.set_setting("block_calendar_id", block_calendar_id)
    with db.connect() as conn:
        conn.execute("update calendars set synced = (id = any(%s))", (synced_calendar,))
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/sync")
def sync_now():
    counts = google.sync()
    return RedirectResponse(f"/settings?synced={counts['events']}", status_code=303)


@app.get("/healthz")
def healthz():
    with db.connect() as conn:
        conn.execute("select 1")
    return {"ok": True}


# --- Agents tab ---------------------------------------------------------------------------------

def _or_404(row):
    if row is None:
        raise HTTPException(404)
    return row


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    return _render(request, "agents.html", channels=agents.channels())


@app.post("/agents/channels")
def agents_new_channel(name: str = Form(...), repo_url: str = Form(""), default_branch: str = Form("main")):
    cid = agents.save_channel(name, repo_url, default_branch)
    return RedirectResponse(f"/agents/c/{cid}", status_code=303)


@app.get("/agents/c/{cid}", response_class=HTMLResponse)
def agents_channel(request: Request, cid: int):
    ch = _or_404(agents.channel(cid))
    return _render(request, "agent_channel.html", ch=ch, members=agents.channel_agents(cid),
                   tasks=agents.channel_tasks(cid), channels=agents.channels())


@app.post("/agents/c/{cid}")
def agents_edit_channel(cid: int, name: str = Form(...), repo_url: str = Form(""), default_branch: str = Form("main")):
    _or_404(agents.channel(cid))
    agents.save_channel(name, repo_url, default_branch, cid)
    return RedirectResponse(f"/agents/c/{cid}", status_code=303)


@app.post("/agents/c/{cid}/agents")
def agents_save_agent(cid: int, name: str = Form(...), kind: str = Form("code"), model: str = Form("sonnet"),
                      role: str = Form(""), aid: int | None = Form(None)):
    _or_404(agents.channel(cid))
    if kind not in agents.TOOLS:
        raise HTTPException(400)
    agents.save_agent(cid, name, kind, model, role, aid)
    return RedirectResponse(f"/agents/c/{cid}", status_code=303)


@app.post("/agents/c/{cid}/tasks")
def agents_new_task(cid: int, text: str = Form(...)):
    _or_404(agents.channel(cid))
    agent, text = agents.pick_agent(agents.channel_agents(cid), text.strip())
    if agent is None or not text:
        raise HTTPException(400, "Add an agent to this channel first")
    return RedirectResponse(f"/agents/t/{agents.start_task(agent['id'], text)}", status_code=303)


@app.get("/agents/t/{tid}", response_class=HTMLResponse)
def agents_thread(request: Request, tid: int):
    t = _or_404(agents.task(tid))
    agents.mark_seen(tid)
    return _render(request, "agent_thread.html", t=t, msgs=agents.messages(tid))


@app.get("/agents/t/{tid}/messages", response_class=HTMLResponse)
def agents_thread_messages(request: Request, tid: int):
    t = _or_404(agents.task(tid))
    if t["status"] not in ("queued", "running"):
        agents.mark_seen(tid)
    return _render(request, "_agent_thread.html", t=t, msgs=agents.messages(tid))


@app.post("/agents/t/{tid}/reply")
def agents_reply(tid: int, text: str = Form(...)):
    if not text.strip() or not agents.reply(tid, text):
        raise HTTPException(409, "Still running: wait for it, or cancel")
    return RedirectResponse(f"/agents/t/{tid}", status_code=303)


@app.post("/agents/t/{tid}/cancel")
def agents_cancel(tid: int):
    agents.cancel(tid)
    return RedirectResponse(f"/agents/t/{tid}", status_code=303)
