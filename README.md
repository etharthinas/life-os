# life-os

Personal productivity app: todos, time blocks, journal, people, finance, LLM wiki. Plan and decisions: `todo.md`.

- `app/main.py`: web (FastAPI + Jinja + HTMX). `app/telegram.py`: bot (long polling). `app/capture.py`: shared logic.
- `migrations/*.sql`: plain Postgres, applied in order on web startup (or `python -m app.db`).
- Data lives in Postgres (Supabase in production), so the VM can be replaced at any time.

## Local dev (no Docker needed)

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
cp .env.example .env
scripts/devdb.sh start            # Postgres 17 on :54329, data in .dev/pgdata
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m app.telegram  # needs TELEGRAM_BOT_TOKEN in .env
.venv/bin/python -m pytest -q
```

## Quick-add syntax

Trailing tokens only: `buy gift for mom fri !1 30m #family`

- due: `today` `tomorrow` `오늘` `내일` `모레`, weekday `mon`…`sun` / `월`…`일` (next occurrence), `10/31`, `2026-10-31`
- `!1`–`!3` priority (1 = high), `30m` / `2h` estimate, `#project`

## Telegram

1. Create a bot with @BotFather, put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Run the bot and message it. It replies with your chat id. Set `TELEGRAM_CHAT_ID` and restart.
   It ignores every other chat.

## Deploy (one VM)

```sh
# on the VM: install Docker + Tailscale, copy the repo and .env (DATABASE_URL = Supabase session pooler URI)
docker compose up -d --build
tailscale serve --bg 8000         # https://<vm>.<tailnet>.ts.net, only your devices can reach it
```

On iPhone, open that URL in Safari → Share → Add to Home Screen.

## Agents tab

Project channels with Claude Code agents in them (`app/agents.py`). The `agents` container takes
one task at a time. Code agents work in a git worktree on `agent/<task id>` and get a PR opened;
research agents get an empty folder and web tools. A reply in a thread continues the same session.

Setup on the VM (once):
1. Run the role SQL in the comment at the end of `migrations/023_agents.sql` (Supabase SQL editor).
2. `cp .env.agents.example .env.agents` and fill it in. Use a fine-grained GitHub token, only for
   the repos agents may touch.
3. Swap, since the box has 1 GB: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`,
   plus `/swapfile none swap sw 0 0` in `/etc/fstab`.
4. `docker compose up -d --build`.

Logs: `docker compose logs -f agents`.
