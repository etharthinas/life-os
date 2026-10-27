"""Postgres access. Plain SQL, no ORM, so the same code runs on local Postgres and Supabase."""

import atexit

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def connect():
    """A pooled autocommit connection; use `conn.transaction()` for multi-statement work.

    Every round trip to the DB costs a lot when it's in another region, so: pooled connections,
    autocommit (no BEGIN/COMMIT round trips) and no health-check query on checkout.
    """
    global _pool
    if _pool is None:
        # prepare_threshold=None: Supabase's transaction pooler doesn't support prepared statements.
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=4, open=True, max_idle=300,
                               kwargs={"row_factory": dict_row, "prepare_threshold": None,
                                       "autocommit": True})
        atexit.register(_pool.close)
    return _pool.connection()


def migrate() -> list[str]:
    """Apply migrations/*.sql that haven't run yet, in filename order. Returns the applied names."""
    applied = []
    with connect() as conn:
        conn.execute("create table if not exists schema_migrations "
                     "(name text primary key, applied_at timestamptz not null default now())")
        done = {r["name"] for r in conn.execute("select name from schema_migrations")}
        for path in sorted((config.ROOT / "migrations").glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                # web and worker start together: one applies, the other waits and then skips it.
                conn.execute("select pg_advisory_xact_lock(2026)")
                if conn.execute("select 1 from schema_migrations where name = %s", (path.name,)).fetchone():
                    continue
                conn.execute(path.read_text())
                conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    names = migrate()
    print("applied:", ", ".join(names) if names else "nothing (up to date)")
