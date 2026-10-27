#!/bin/sh
# Local Postgres 17 for development (same major version as Supabase). No Docker/Homebrew needed.
#   scripts/devdb.sh start | stop | status | psql
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BIN=$ROOT/.dev/pgbin/bin
DATA=$ROOT/.dev/pgdata
PORT=54329
V=17.11.0

if [ ! -x "$BIN/postgres" ]; then
  echo "downloading Postgres $V binaries (zonky embedded-postgres, Maven Central)..."
  mkdir -p "$ROOT/.dev/pgbin" && cd "$ROOT/.dev/pgbin"
  URL=https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-darwin-arm64v8/$V/embedded-postgres-binaries-darwin-arm64v8-$V.jar
  curl -sSfLo pg.jar "$URL"
  [ "$(shasum pg.jar | cut -d' ' -f1)" = "$(curl -sSfL "$URL.sha1")" ] || { echo "checksum mismatch"; exit 1; }
  unzip -o -q pg.jar '*.txz' && tar xJf ./*.txz && rm -f pg.jar ./*.txz
fi

case "${1:-start}" in
  start)
    if [ ! -d "$DATA" ]; then
      "$BIN/initdb" -D "$DATA" -U postgres --auth=trust --encoding=UTF8 --locale=C >/dev/null
    fi
    "$BIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1 \
      || "$BIN/pg_ctl" -D "$DATA" -o "-p $PORT -k /tmp" -l "$ROOT/.dev/pg.log" -w start
    # The bundle has no psql/createdb, so use the venv's psycopg.
    "$ROOT/.venv/bin/python" - "$PORT" <<'PY'
import sys, psycopg
with psycopg.connect(f"postgresql://postgres@localhost:{sys.argv[1]}/postgres", autocommit=True) as c:
    if not c.execute("select 1 from pg_database where datname = 'lifeos'").fetchone():
        c.execute("create database lifeos")
PY
    echo "DATABASE_URL=postgresql://postgres@localhost:$PORT/lifeos"
    ;;
  stop) "$BIN/pg_ctl" -D "$DATA" stop ;;
  status) "$BIN/pg_ctl" -D "$DATA" status ;;
  sql)  # run SQL from stdin, print rows: scripts/devdb.sh sql <<< "select * from todos"
    exec "$ROOT/.venv/bin/python" - "$PORT" 3<&0 <<'PY'
import os, sys, psycopg
sql = os.fdopen(3).read()
with psycopg.connect(f"postgresql://postgres@localhost:{sys.argv[1]}/lifeos", autocommit=True) as c:
    for stmt in filter(str.strip, sql.split(";")):
        try:
            cur = c.execute(stmt)
            for row in cur.fetchall() if cur.description else []:
                print(row)
        except psycopg.Error as e:
            print("ERROR:", e.diag.message_primary)
PY
    ;;
  *) echo "usage: $0 start|stop|status|sql"; exit 1 ;;
esac
