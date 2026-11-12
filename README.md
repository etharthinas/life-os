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
4. Firewall, so agents can't reach the tailnet, the host (`tailscale serve` on :443) or Lightsail
   metadata. The `agents` network is `172.30.0.0/24` (docker-compose.yml). On the VM this is the
   systemd unit `agents-firewall.service` (enabled, runs after docker), which adds:
   ```sh
   iptables -I DOCKER-USER -s 172.30.0.0/24 -d 100.64.0.0/10 -j DROP
   iptables -I DOCKER-USER -s 172.30.0.0/24 -d 169.254.169.254 -j DROP
   iptables -I INPUT -s 172.30.0.0/24 -j DROP   # every host address, incl. the bridge gateway
   ```
   Check: from the agents container, `web` doesn't resolve and the host/tailnet IPs time out.
5. Add `agents` to `COMPOSE_PROFILES` in `.env` (e.g. `COMPOSE_PROFILES=telegram,agents`), then
   `docker compose up -d --build`.

Per-channel secrets: files in `/work/env/<channel id>/` (e.g. `.env`) are copied into each task's
worktree and git-excluded, so they're never committed. SSH: the agents' own key, `~/.ssh/config`,
`known_hosts` and the AWS profile for SSM live in the `agents-home` volume (`/home/agent`); which
hosts a channel may use is stated in its agents' roles.

Logs: `docker compose logs -f agents`.

Security notes:
- Give agents a **separate Telegram bot**. An agent can read its env, and with the main bot's token
  it could take over the capture bot's `getUpdates`.
- Turn on branch protection (or a ruleset) on the default branch of every repo the GitHub token
  can reach: the token can push to main.
- Treat everything in `.env.agents` as readable by any agent.
- Changing a channel's repo URL after its first task: remove `/work/repos/<channel id>` first.
