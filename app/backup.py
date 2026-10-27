"""Nightly database backup: `pg_dump` of the whole DB, gzipped, into object storage under
backups/YYYY-MM-DD.sql.gz. Journals are editable (no version history), so this is the undo."""

import gzip
import subprocess
from datetime import datetime

from . import config, storage


def run() -> str:
    dump = subprocess.run(["pg_dump", "--no-owner", "--no-privileges", config.DATABASE_URL],
                          check=True, capture_output=True).stdout
    key = f"backups/{datetime.now(config.TZ):%Y-%m-%d}.sql.gz"
    storage.store().put(key, gzip.compress(dump), "application/gzip")
    return f"{key} ({len(dump) / 1e6:.1f} MB raw)"


if __name__ == "__main__":
    print(run())
