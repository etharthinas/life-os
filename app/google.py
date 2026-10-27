"""Google Calendar: OAuth (desktop loopback + PKCE), sync into Postgres, and writing time blocks.

Connect once from a machine with a browser:   python -m app.google auth
The refresh token is stored in the DB, so the server can sync without a browser.
Blocks go into my existing calendars, tagged with extendedProperties.private.lifeos_block.
"""

import base64
import hashlib
import http.server
import json
import secrets
import sys
import time
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta

import httpx

from . import config, db

SCOPES = "openid email https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events"
API = "https://www.googleapis.com/calendar/v3"
SYNC_PAST_DAYS, SYNC_FUTURE_DAYS = 7, 30

_http = httpx.Client(timeout=30)
_tokens: dict[str, tuple[str, float]] = {}  # email -> (access token, expiry)


def _client():
    if not (config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET):
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set")
    return config.GOOGLE_CLIENT_ID, config.GOOGLE_CLIENT_SECRET


# --- OAuth -------------------------------------------------------------------------------------

def authorize() -> str:
    """Interactive login in the local browser. Stores the refresh token and returns the email."""
    client_id, client_secret = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("life-os connected. You can close this tab.".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Opening the browser. If it doesn't open, visit:\n" + url, flush=True)
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if "error" in result:
        raise RuntimeError(f"Google login failed: {result['error']}")

    r = _http.post("https://oauth2.googleapis.com/token", data={
        "code": result["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    r.raise_for_status()
    tok = r.json()
    if "refresh_token" not in tok:
        raise RuntimeError("Google returned no refresh token; remove the app's access and retry")
    payload = tok["id_token"].split(".")[1]
    email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["email"]
    with db.connect() as conn:
        conn.execute("insert into google_accounts (email, refresh_token) values (%s, %s) "
                     "on conflict (email) do update set refresh_token = excluded.refresh_token",
                     (email, tok["refresh_token"]))
    return email


def _access_token(email: str, refresh_token: str) -> str:
    token, expiry = _tokens.get(email, ("", 0))
    if time.time() < expiry - 60:
        return token
    client_id, client_secret = _client()
    r = _http.post("https://oauth2.googleapis.com/token", data={
        "refresh_token": refresh_token, "client_id": client_id, "client_secret": client_secret,
        "grant_type": "refresh_token",
    })
    r.raise_for_status()
    tok = r.json()
    _tokens[email] = (tok["access_token"], time.time() + tok["expires_in"])
    return tok["access_token"]


def _api(account: dict, method: str, path: str, **kw) -> dict:
    headers = {"Authorization": f"Bearer {_access_token(account['email'], account['refresh_token'])}"}
    r = _http.request(method, API + path, headers=headers, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def accounts() -> list[dict]:
    with db.connect() as conn:
        return conn.execute("select * from google_accounts").fetchall()


# --- Sync --------------------------------------------------------------------------------------

def _parse_time(t: dict) -> tuple[datetime, bool]:
    if "dateTime" in t:
        return datetime.fromisoformat(t["dateTime"]), False
    return datetime.combine(date.fromisoformat(t["date"]), datetime.min.time(), config.TZ), True


def sync() -> dict:
    """Refresh the calendar list and replace events in the sync window. Returns counts."""
    today = datetime.now(config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    lo, hi = today - timedelta(days=SYNC_PAST_DAYS), today + timedelta(days=SYNC_FUTURE_DAYS)
    counts = {"calendars": 0, "events": 0}
    for acc in accounts():
        cals = _api(acc, "GET", "/users/me/calendarList")["items"]
        with db.connect() as conn:
            conn.cursor().executemany(
                "insert into calendars (id, account, summary, color, access_role, is_primary) "
                "values (%s, %s, %s, %s, %s, %s) on conflict (id) do update set "
                "summary = excluded.summary, color = excluded.color, "
                "access_role = excluded.access_role, is_primary = excluded.is_primary",
                [(c["id"], acc["email"], c.get("summaryOverride") or c["summary"], c.get("backgroundColor"),
                  c["accessRole"], bool(c.get("primary"))) for c in cals],
            )
            synced = [r["id"] for r in conn.execute(
                "select id from calendars where account = %s and synced", (acc["email"],))]
        rows = []
        for cal_id in synced:
            params = {"timeMin": lo.isoformat(), "timeMax": hi.isoformat(), "singleEvents": "true",
                      "maxResults": "2500"}
            while True:
                page = _api(acc, "GET", f"/calendars/{urllib.parse.quote(cal_id)}/events", params=params)
                for e in page.get("items", []):
                    if e.get("status") == "cancelled" or "start" not in e:
                        continue
                    start, all_day = _parse_time(e["start"])
                    end, _ = _parse_time(e["end"])
                    block = (e.get("extendedProperties", {}).get("private", {}).get("lifeos_block"))
                    rows.append((cal_id, e["id"], e.get("summary"), start, end, all_day,
                                 e.get("transparency") == "transparent",
                                 int(block) if block and block.isdigit() else None, e.get("htmlLink")))
                if not page.get("nextPageToken"):
                    break
                params["pageToken"] = page["nextPageToken"]
        with db.connect() as conn, conn.transaction():
            conn.execute("delete from events where calendar_id = any(%s) and start_at < %s and end_at > %s",
                         (synced, hi, lo))
            conn.cursor().executemany(
                "insert into events (calendar_id, event_id, summary, start_at, end_at, all_day, "
                "transparent, lifeos_block, html_link) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (calendar_id, event_id) do nothing",
                rows,
            )
        counts["calendars"] += len(synced)
        counts["events"] += len(rows)
    return counts


# --- Blocks ------------------------------------------------------------------------------------

def write_block(block: dict, title: str, calendar_id: str) -> str:
    """Create the block as an event in an existing calendar. Returns the Google event id."""
    with db.connect() as conn:
        acc = conn.execute("select a.* from google_accounts a join calendars c on c.account = a.email "
                           "where c.id = %s", (calendar_id,)).fetchone()
    event = _api(acc, "POST", f"/calendars/{urllib.parse.quote(calendar_id)}/events", json={
        "summary": title,
        "start": {"dateTime": block["start_at"].isoformat()},
        "end": {"dateTime": block["end_at"].isoformat()},
        "description": "Planned by life-os",
        "extendedProperties": {"private": {"lifeos_block": str(block["id"]),
                                           "lifeos_todo": str(block["todo_id"])}},
    })
    with db.connect() as conn:
        conn.execute(
            "insert into events (calendar_id, event_id, summary, start_at, end_at, lifeos_block, html_link) "
            "values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
            (calendar_id, event["id"], title, block["start_at"], block["end_at"], block["id"], event.get("htmlLink")),
        )
    return event["id"]


if __name__ == "__main__":
    if sys.argv[1:] == ["auth"]:
        db.migrate()
        print("connected:", authorize())
        print("synced:", sync())
    elif sys.argv[1:] == ["sync"]:
        print(sync())
    else:
        raise SystemExit("usage: python -m app.google auth|sync")
