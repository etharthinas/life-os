"""Background jobs. Run with `python -m app.worker`.

Quick checks (re-tagging faces after a correction) run every few seconds; the rest every 10 minutes.
"""

import time
import traceback
from datetime import datetime

from . import backup, capture, config, days, db, faces, google

TICK = 15
SYNC_EVERY = 10 * 60
BACKUP_HOUR = 3  # local time


def slow_jobs() -> None:
    now = datetime.now(config.TZ).isoformat(timespec="seconds")
    try:
        if google.accounts():
            counts = google.sync()
            print("calendar sync:", counts, flush=True)
            days.set_setting("last_sync", {"at": now, "ok": True, "events": counts["events"]})
    except Exception as e:
        traceback.print_exc()
        # invalid_grant = the Google login expired (Testing-mode apps: every 7 days).
        expired = "invalid_grant" in getattr(getattr(e, "response", None), "text", "")
        days.set_setting("last_sync", {"at": now, "ok": False, "expired": expired, "error": repr(e)[:300]})
    try:
        # New photos: find faces (in a subprocess, to give the models' memory back), regroup, re-tag.
        if faces.available() and faces.pending():
            n = faces.scan_in_subprocess()
            print("faces: scanned", n, "photos;", faces.relabel(recluster=True), flush=True)
    except Exception:
        traceback.print_exc()
    try:
        if n := capture.process_stale():
            print("captures sorted:", n, flush=True)
    except Exception:
        traceback.print_exc()
    try:
        local = datetime.now(config.TZ)
        done = (days.get_setting("last_backup") or {}).get("date")
        if local.hour >= BACKUP_HOUR and done != local.date().isoformat():
            print("backup:", backup.run(), flush=True)
            days.set_setting("last_backup", {"date": local.date().isoformat(), "at": now})
    except Exception:
        traceback.print_exc()


def run() -> None:
    db.migrate()
    print("worker started", flush=True)
    last_slow = 0.0
    while True:
        if time.monotonic() - last_slow >= SYNC_EVERY:
            last_slow = time.monotonic()
            slow_jobs()
        try:
            if faces.take_relabel_request():
                print("faces:", faces.relabel(), flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(TICK)


if __name__ == "__main__":
    run()
