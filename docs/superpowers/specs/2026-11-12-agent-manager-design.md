# Agent manager: design

A Slack-like "Agents" tab in life-os. Each project is a channel, agents live in a channel, and I
give them tasks from desktop or phone. Runs on the existing Lightsail box.

## Decisions

| Topic | Decision |
|---|---|
| Where | New `/agents` tab in the life-os web app (same PWA, same Tailscale URL) |
| Agent kinds | Per agent: `code` (repo edits, Bash, git) or `research` (web search/fetch, no repo) |
| Capacity | One task runs at a time; the rest wait in a queue. 1 GB box + 2 GB swap |
| Code output | Branch `agent/<task-id>` per task, pushed, GitHub PR opened, link posted |
| Memory | One thread = one task = one Claude session. A reply in the thread resumes it (`--resume`). New thread = fresh session |
| Notifications | Existing Telegram bot: finished / failed / waiting for reply, with link to the thread |
| LLM | `claude -p` on the subscription (`CLAUDE_CODE_OAUTH_TOKEN`), as elsewhere in life-os |

## Data (`migrations/023_agents.sql`)

- `agent_channels`: `id`, `name` (unique), `repo_url` (null for research-only channels),
  `default_branch` (default `main`), `default_agent_id`, `created_at`
- `agents`: `id`, `channel_id`, `name` (unique within a channel), `role` (system prompt text),
  `kind` (`code` | `research`), `model` (default `sonnet`), `created_at`
- `agent_tasks`: `id`, `agent_id`, `title` (first line of the request), `status`
  (`queued` | `running` | `done` | `failed` | `cancelled`), `session_id`, `branch`, `pr_url`,
  `error`, `queued_at`, `started_at`, `finished_at`, `seen_at` (for unread dots)
- `agent_messages`: `id`, `task_id`, `author` (`me` | `agent` | `system`), `body`, `created_at`

A follow-up reply sets the task back to `queued`. The runner sees a `session_id` and resumes.

## Runner (`app/agents.py`, run as `python -m app.agents`)

- New `agents` service in `docker-compose.yml`, built from the same image.
- Loop: claim the oldest `queued` task with `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)`
  and mark it `running`. If nothing is queued, sleep 3 s.
- Code agents:
  - The repo is cloned once into the `agents-work` volume at `/work/<channel>`.
  - Each task gets `git worktree add /work/<channel>-wt/<task-id> -b agent/<task-id>`, based on
    the fetched `origin/<default_branch>`.
  - The runner commits whatever the agent left uncommitted, pushes the branch, and runs
    `gh pr create --fill` (or finds the PR that already exists on a follow-up turn).
  - If there's no diff, no PR is opened and the agent's last message stands.
- Research agents: the working directory is an empty temp dir.
- Command: `claude -p --output-format stream-json --verbose --model <model>
  --append-system-prompt <role> --allowedTools <preset> [--resume <session_id>]`, with the request
  on stdin and `--permission-mode acceptEdits`.
  - Preset `code`: `Read Edit Write Glob Grep Bash`.
  - Preset `research`: `WebSearch WebFetch Read`.
- Stream handling:
  - The `session_id` from the `system/init` event is saved.
  - Each `assistant` text block becomes an `agent` message as it arrives.
  - Tool calls become one short `system` line each (e.g. `Edit app/main.py`) so the thread shows
    progress.
  - The final `result` event decides `done` or `failed`.
- Timeout 30 min per turn (`AGENT_TIMEOUT_MIN`). On timeout or cancel, the process group is killed
  and the task is marked `failed` / `cancelled`.
- Cancel: the web app sets `status = 'cancelled'`. The runner checks every few seconds while a
  task runs and kills the process.
- On startup, any task left `running` from a crash is set back to `failed` with
  `error = 'runner restarted'`.
- Telegram: after each turn, `Bot.send` to `TELEGRAM_CHAT_ID` sends
  `"<channel>/<agent> done: <title>"` plus the PR link and thread URL. If no bot token is set,
  nothing is sent.
- Nightly: worktrees of tasks finished more than 7 days ago are removed (`git worktree remove`).
  Their branches stay on GitHub.

## Security

Agents run Bash, so the `agents` container gets its own env file, `.env.agents`, with only:

- `CLAUDE_CODE_OAUTH_TOKEN`
- `GH_TOKEN`: a fine-grained token for the chosen repos only (contents + pull requests write)
- `DATABASE_URL` for a Postgres role `agents_runner` that has rights on the four `agent_*` tables
  only
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PUBLIC_URL`

No life-os `.env`, Google tokens, R2 keys, or Docker socket. The process runs as a non-root user.
The web app keeps its own full-access DB connection.

## UI (`/agents`)

- Desktop: three columns.
  - Left: channel list with unread dots.
  - Middle: channel feed of task threads, newest last, each with a status chip
    (queued / running / PR #n / failed).
  - Right: the open thread.
- Phone (narrow): one screen at a time. Channels, then channel, then thread. Back links at the top.
  Composer pinned to the bottom.
- Composer:
  - In a channel, a new message starts a task. A leading `@name` picks the agent; otherwise it
    goes to the channel's default agent.
  - In a thread, a message is a follow-up turn.
  - Enter sends; Shift-Enter makes a new line.
- A running or queued thread polls with HTMX every 2 s (`hx-trigger="every 2s"`) and stops once it
  has finished.
- Thread header: Cancel (while queued/running), PR link, branch.
- Settings in the channel header: add/edit channel (name, repo URL, default branch) and agents
  (name, kind, model, role). Plain forms, no separate admin page.
- New nav item in `base.html`. Same styles as the rest of the app.

## Deploy

- `docker-compose.yml`: `agents` service with `env_file: .env.agents`, volume
  `agents-work:/work`, `restart: unless-stopped`, logging anchor.
- Dockerfile: add `git` and `gh`, plus a non-root `agent` user for the agents service.
- On the box (one time): 2 GB swapfile; create `.env.agents`; create the `agents_runner` DB role
  (SQL in the migration's comments, run by hand since the migrations run as the owner).

## Tests (`tests/test_agents.py`)

- Two runners claiming at once get different tasks (or none); never the same task.
- Stream parsing: a fake `claude` script on `PATH` prints canned stream-json. The task ends with
  the right messages, `session_id`, and status.
- Follow-up turn passes `--resume <session_id>`.
- Cancel while running ends with status `cancelled` and a killed process.

## Out of scope (add when needed)

- Parallel agents (when the queue gets in the way; needs a bigger box)
- Agent-to-agent handoff, scheduled/recurring tasks
- Web push (Telegram covers it)
- Account switching on rate limit (crew does this; for now a rate-limited turn fails with the
  reason)
- Streaming token-by-token (per-message polling is enough)
