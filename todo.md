# life-os

I'm going to make an all-in-one productivity app.
    1. It will read from my calendar and will manage my timeblocks.
    2. It will record various todos and assign me in timeblocks. (Linking to Notion and Slack to automatically find the tasks can also be thought of) + notifications
    3. Like my obsidian in icloud, it will log daily events and also manage people.
    4. I also want to link my finance as well.
    5. It will also be the single-source of truth for all my content. I'm thinking something like an llm-wiki. Human input is preserved, but the llm will parse from it and create a wiki by itself.

---

## Decisions (updated 2026-10-23)

| Topic | Decision |
|---|---|
| Interface | Custom web app, installable on iPhone as a PWA |
| Compute | **AWS Lightsail $7 plan (1 GB) in Sydney** (ap-southeast-2), IPv4 + disk included, ~₩10k/month. Managed over SSH (`ssh lifeos`, key in `~/.ssh/lifeos-lightsail.pem`). SSH goes over Tailscale (`HostName lifeos.tail969156.ts.net`, since 2026-10-26), so it works from any network; the public port 22 stays open only to the office IP. Tailscale name `lifeos.tail969156.ts.net`. App in `/srv/life-os`, deployed by rsync + `docker compose up -d --build`. The AWS API is unusable with IAM keys: an organization policy above the account (o-hl6rwb1lsc) denies Lightsail writes in every region, so AWS-level changes are done by hand in the console. GCP dropped after a billing error, EC2 (~$13) too pricey. Everything runs in Docker so it can move between clouds |
| DB | **Supabase** (managed Postgres) |
| Files/images | **S3-compatible object storage** (Cloudflare R2 recommended, see below) |
| Network | Tailscale (only my devices can reach the server) |
| LLM | Existing Claude Code subscription via headless `claude -p` on the server. No API spend |
| Calendar | Google Calendar. App **proposes** time blocks, I approve, then it writes |
| Notifications | Telegram bot (push + quick capture + approve buttons) |
| Obsidian | Import once, then the app replaces it |
| Task sources | Notion + Slack, **my personal workspaces only** |
| Finance | **KB국민은행**. **No automatic entry for now** (no payment capture, no brokerage sync). Manual entry + Excel import. Budgets & insights, planning & goals, manual net worth |
| People | Notes & history, dates & gifts, keep-in-touch reminders |
| Phone | iPhone |

## Avoiding lock-in

- All data lives in Supabase + object storage, so the server holds no data and can be thrown away. Moving machines = `docker compose up` + an `.env` file
- Plain Postgres + SQL migrations in the repo (no Supabase-only features in the core), so the DB could move to any Postgres with `pg_dump`
- Object storage only through the S3 API, so R2 / AWS S3 / Supabase Storage are interchangeable
- Nothing runs on or depends on the work Mac

## Hosting: one VM, not ECS

ECS/Fargate doesn't fit: `claude -p` needs a persistent login (`~/.claude`), and the bot and worker run all the time anyway. So a single VM running `docker compose` is the simplest and cheapest option.

| Option | Cost | Notes |
|---|---|---|
| **GCP e2-micro (Recommended to start)** | $0 (Always Free, only in us-west1 / us-central1 / us-east1) | 1 GB RAM + 30 GB disk. Enough for web + bot; add 2 GB swap for `claude -p`. ~150 ms from Seoul, fine for this |
| GCP e2-small / AWS t4g.small (Seoul) | ~$13–17/mo (+ ~$3.6/mo for a public IPv4 on AWS) | Upgrade path if 1 GB gets tight. AWS fits since I already use the AWS CLI + SSM |
| ECS Fargate | ~$10+/mo for 0.25 vCPU always on | More moving parts, no persistent home dir → not worth it |

Free-tier details change, so check current terms when creating the VM. Moving clouds later = new VM + `docker compose up` + `.env`.

## Ways to keep it cheap

Target: **about ₩0/month** (GCP free VM + Supabase free + R2 free + existing Claude subscription).

1. **Supabase free tier:** 500 MB DB is plenty for text/transactions. Free projects pause after ~1 week of no activity, but the worker's regular polling keeps it active. Upgrade to Pro ($25/mo) only if it's ever outgrown
2. **Cloudflare R2 for images:** 10 GB free, no egress fees, S3-compatible API. Supabase Storage free is only 1 GB, and AWS S3 has no permanent free tier (though it's still only pennies a month)
3. **Boring stack, no build step:** FastAPI + Jinja + HTMX in Python (same as `crew`), packaged with Docker Compose
4. **LLM only where it adds value, in batches:** rules and plain code for anything deterministic. `claude -p --model haiku` in batches for small jobs. Heavy jobs (wiki compile, monthly review) run overnight. Results are cached by input hash so unchanged input is never reprocessed
5. **Polling instead of webhooks:** Telegram long-polling, plus Google/Notion/Slack polled every 10–15 min, so no public URL is needed
6. **Free APIs only:** Google Calendar, Telegram, Notion, Slack are free at personal scale

## Architecture

```
iPhone (PWA, Telegram, Shortcuts) ──Tailscale──▶ Server (Docker Compose)
                                                   ├─ web (FastAPI + HTMX)
                                                   ├─ worker: pollers, scheduler, job queue
                                                   │     └─ claude -p (subscription)
                                                   │
                         Supabase Postgres ◀───────┤  (all structured data)
                         Cloudflare R2 (S3) ◀──────┘  (images, attachments, raw files)
```

## Modules

### 1. Calendar & time blocks
- [ ] Google OAuth (refresh token stored in the DB, encrypted)
- [ ] Sync events: read all my calendars, write only to a dedicated **"life-os" calendar**
- [ ] Day/week view: events + proposed blocks + free gaps
- [ ] Morning plan (07:00): greedy planner fits todos into free gaps (priority, estimate, deadline). LLM only for estimates/priorities → Telegram ✅ Accept / ✏️ Edit → write to Google on accept
- [ ] When a block is missed, ask in Telegram: "done / push to tomorrow / drop"

### 2. Todos & task intake
- [ ] Inbox: quick add from the app, Telegram (`/t buy gift for mom fri`), and an iOS Share Sheet shortcut
- [ ] Fields: title, project, priority, estimate, deadline, source, status, linked person
- [ ] Notion poller (my workspace): internal integration token, shared with my task databases. Dedupe by page id (`todos.source_ref`)
- [ ] Slack poller (my workspace): user token. Pull saved items + messages that mention me
- [ ] LLM step (haiku, batched): raw items → tasks with estimates. Dedupe against existing tasks
- [ ] Notifications: block start, deadline today, daily summary, inbox needs triage

### 3. Journal, people & the wiki ("llm-wiki")
- **Layer 1, raw (human input, never edited by the LLM):** daily logs, quick notes, imported Obsidian notes, Telegram captures. Append-only. Text in Postgres, attachments in R2
- **Layer 2, wiki (LLM-maintained):** pages for people, projects, places, topics, plus an index. Each statement links back to its raw source entries
- [ ] Nightly compile: only take new raw entries since the last run → `claude -p` updates the affected pages → keep each page's revision history so I can review/revert
- [ ] **One-time Obsidian import:** the server can't read iCloud (no official API), but the work Mac is now signed in to iCloud. So run the importer **once from the work Mac** to push the vault to Supabase + R2. Only this one run uses the work Mac; nothing ongoing depends on it. Vault "Jay's Obsidian" as of 2026-10-23:
  - `Daily/YYYY/MM/YYYY.MM.DD.md` (863 notes, 2021–2026) → raw journal entries. Frontmatter `Date`, `People: [[...]]`, section `## 사건`. `People:` gives the person↔day links for "last contact" right away
  - `Entities/` (211) → people/groups (`type: person|group`, `Birthday`, `Major`, `Class of`, `Phone`, `Associated`, `members`) → seed people pages directly, no LLM needed
  - `Notes/` (419) → zettels (`type: concept`, `up`, `related`, `instance-of`, `part-of`, `opposes`, `property-of`) → keep these typed links as wiki edges
  - Also `Inbox.md`, `Inbox from Notion.md` → todos inbox. Weekly reviews (`GGGG-Www`). 5 `.base` views (People, Daily, Entities, Orphans…) → rebuild as app views
  - Attachments: ~620 images (jpeg/png/heic) + a few others → R2. Skip `.ajson` (~819 files of plugin embedding cache) and `.obsidian/`
  - Files are iCloud "dataless" (stored online only), so the importer must download each one first (`brctl download` or just read it)
  - Plugins in use: dataview, templater, homepage, omnisearch → nothing to migrate except the templates' structure
- [x] People pages with structured fields: birthday, gift ideas, last contact, contact frequency target
- [ ] Anniversaries / other dates per person
- [ ] Keep-in-touch: last contact = latest mention in a log (done) or a calendar event with them (todo) → Telegram nudge when overdue (todo, needs the bot)
- [ ] Dates: birthday/anniversary reminders 7 days and 1 day before, with gift ideas from their page
- [ ] Search: Postgres full-text search over raw + wiki (pgvector later if needed). "Ask" box → `claude -p` with the matching pages

### 4. Finance (KB국민은행 + brokerage)
No automatic payment capture (decided 2026-10-23). Data comes in through:
- [ ] **Excel import:** upload the KB 인터넷뱅킹 / 국민카드 transaction export (monthly or whenever). Dedupe by date + amount + merchant + balance
- [ ] **Quick manual entry:** `/won 12000 점심 #food` in Telegram for cash or anything else worth noting
- [ ] **History seed:** import past Money Manager (Realbyte) data from its iCloud backup, if the format can be read
- [ ] ~~Brokerage sync~~ — dropped for now. Holdings entered by hand in a monthly net-worth snapshot
- [ ] Categorization: merchant→category rules first, LLM only for unknown merchants, then it learns the rule
- [ ] Net worth: daily snapshot of accounts + holdings → trend chart, asset allocation
- [ ] Budgets: monthly per-category budget, mid-month pace alert in Telegram
- [ ] Insights: recurring/subscription detection, unusual spending, monthly LLM review saved as a wiki page
- [ ] Planning: savings goals, simple cash-flow forecast (fixed costs + salary), 연말정산 summary
- Security: Supabase row-level security on (no policies, so the public REST API sees nothing), DB password only on the server, brokerage keys encrypted, Tailscale-only access

### 5. Platform
- [ ] Repo: `app/` (web), `worker/` (pollers, queue, scheduler), `jobs/` (prompt templates), `migrations/` (SQL), `docker-compose.yml`
- [ ] Job queue table in Postgres: kind, input hash, status, model, output. One `claude -p` at a time
- [ ] Back off when the subscription is near its limit (Claude Code status line `rate_limits`, same approach as `crew`)
- [ ] Nightly `pg_dump` → R2 (Supabase free tier has no backups you can download)
- [ ] PWA manifest + service worker (offline read of today's plan). HTTPS via `tailscale serve`

## Build order (MVP first)
1. **Skeleton** — in progress
   - [x] FastAPI + HTMX web app: todos (quick-add syntax) + journal log, PWA manifest
   - [x] Telegram bot (long polling): `/t`, `/todos`, `/done`, `/log`, plain text = log, single-chat allowlist
   - [x] SQL migrations (`raw_entries` append-only via trigger, `todos`, RLS on)
   - [x] Local dev Postgres 17 without Docker (`scripts/devdb.sh`), Dockerfile + compose for the VM
   - [x] Supabase project created, migrations applied, RLS confirmed, connection pool (2026-10-23)
   - [ ] Telegram bot token from @BotFather → test the bot for real
   - [x] Lightsail instance (Sydney) with swap + Docker + Tailscale, web app running in Docker (2026-10-25)
   - [x] Tailscale Serve on: https://lifeos.tail969156.ts.net (tailnet only) (2026-10-25)
   - [ ] Add to iPhone home screen
   - Supabase stays in Tokyo (decided 2026-10-25). ~110 ms per query from Sydney, so keep queries per request low
   - [ ] Install Claude Code on the server + log in
   - [x] `scripts/deploy.sh` (tests → rsync → compose → health check; `--env` also copies .env)
2. **Calendar** — in progress
   - [x] Blocks go into my **existing** calendars (no separate life-os calendar), tagged `extendedProperties.private.lifeos_block` so the app only touches its own events. Target calendar picked in Settings (default: primary)
   - [x] OAuth (desktop client + PKCE, `python -m app.google auth` on the Mac), refresh token in DB
   - [x] Sync: calendar list + events −7…+30 days, every 10 min in the `worker` container. Settings chooses which calendars count as busy
   - [x] Day view `/day/YYYY-MM-DD`: all-day + timed events, proposed blocks (✓ / ×, accept all), that day's notes
   - [x] Greedy planner (urgent → priority → due → oldest, first fit, skips past time)
   - [x] Connected etharthinas@gmail.com (2026-10-25): 5 calendars, server syncs by itself. Sync status + "login expired" warning in Settings/Day
   - [ ] OAuth app is in **Testing** → login expires every 7 days. Publishing needs a home page + privacy policy link (plan: one-page GitHub Pages site)
   - [ ] First real block write into Google Calendar (not yet tested end to end)
   - [ ] Morning plan push via Telegram (once the bot is on)
   - [ ] Encrypt the refresh token at rest
3. **Journal + Obsidian import + nightly wiki compile** — import in progress
   - [x] Importer `python -m app.obsidian <vault>`: one immutable raw entry per note, people from `Entities/`, mentions from `People:` + `[[links]]`
   - [x] People list (sorted by last contact, birthdays), person timeline, note view, search (trigram index)
   - [x] Imported into Supabase (2026-10-25): 1,497 notes (863 daily, 2021–2026), 453 people (211 with an Entities/ note, the rest from daily `People:` lists), 3,168 mentions
   - Daily note date = filename (frontmatter had a typo: `Daily/2021/02/2021.02.02.md` says 2002-02-02)
   - Re-running the importer skips existing notes and reports notes changed since import (raw entries never get rewritten)
   - [x] Images (2026-10-26): `media` table, files in object storage keyed by sha256: original + `display` (1600 px) + `thumb` (480 px) WebP, EXIF-rotated, no EXIF in the copies. Importer uploads embedded images (609, 1.57 GB originals) and links them to notes (`raw_media`). Embeds and `[[links]]` render in notes/day view. Photos can be added to logs from the app
   - [x] **Storage = Cloudflare R2**, bucket `life-os` (originals are 1.6 GB, over Supabase Storage's 1 GB free). 609 images uploaded and linked in production (2026-10-26). The server keeps a disposable disk cache of thumbs/display copies
   - [x] Fixed NFD/NFC duplicates: macOS file names are NFD, so 179 Korean people existed twice (Entities row without mentions + daily-list row with them). Migration 004 merges them (453 → 274 people) and normalizes titles; importer compares names in NFC
   - [x] People tab (2026-10-26): avatars (profile photo or initials), filter chips, add person, "Birthdays · next 30 days" and "Catch up" sections. Person page: stats, log + photos about them, editable birthday/aliases/catch-up goal, gift ideas, group members, "often with", photo gallery + viewer with "use as photo", timeline with thumbnails. `[[Name]]` in a log mentions that person
   - [ ] Nightly wiki compile with `claude -p`
4. **Finance v1:** KB Excel import + manual entry + categories + budgets
5. **Finance v2:** manual net-worth snapshots, goals, monthly review
6. **Notion/Slack intake** (my workspaces)

## Design (2026-10-26)
- Pocket-diary look: Korean serif (Gowun Batang) for dates and names, Pretendard for UI, indigo-ink accent, light + dark tokens in `app/static/style.css`. Bottom tab bar: Today · Todos · People · Search (Settings from the gear on Today)
- Home = the day page: week strip, schedule, the day's todos (due + overdue + done that day), journal (add with photos, backfill past days), people seen, daily note with light Markdown
- Static files load as `?v=<hash>` (cached forever, new URL each deploy), so the iPhone PWA never keeps an old stylesheet

## Next (decided 2026-10-26)
- [x] **Desktop app** (`desktop/`, Electron): window onto the server + ⌘⇧Space quick capture + menu-bar icon. Installed in /Applications
- [x] **Journal = daily note**: one editable document per day (imported daily note, or created in the app). Quick captures append timestamped paragraphs; photos are `![[…]]` embeds; mentions/photos re-derived from the text on every save. **Edits overwrite** (no version history)
- [x] **Nightly backup**: worker runs `pg_dump` at 03:00 KST → R2 `backups/YYYY-MM-DD.sql.gz`
- [x] **Scratchpad capture** (2026-10-26): Todos tab / day page / desktop ⌘⏎ / Telegram `/t` → `captures` row → Claude (`app/claude.py`, `claude -p` with JSON schema, Sonnet) adds todos with dates/priority/notes + a question if unclear; Undo removes the batch. If Claude fails (limit, outage) it falls back to one todo per line; the worker picks up captures stuck pending.
- [ ] Pick up tasks written in journals (same pipeline, source='journal')
- [ ] **JARVIS**: **desktop wake word** ("Jarvis") in the Electron app, **free first**: on-device wake word + speech-to-text on the Mac, Claude on the server with tools for the app's data, macOS voice for replies
- [ ] Prerequisite for both: Claude Code on the server, authenticated with a `claude setup-token` token (CLAUDE_CODE_OAUTH_TOKEN in .env)
- [ ] **Slack + Notion intake** (personal workspaces): needs a Notion internal integration token and a Slack user token

## Still open
- [ ] 1 GB + swap enough once `claude -p` runs nightly jobs? Otherwise the $12 (2 GB) plan
- [x] **Daily log = one card** (2026-10-26): text + "Add more…" + Edit in one place; old separate `log` entries fold into the day's journal (`journal.merge_logs()`, one-off).
- [x] **People picker + people by id** (2026-10-26): names no longer unique (`people.about` tells them apart); picked people are `mentions.picked` and survive text re-syncs; `[[Name]]` resolves to the picked person of that name. New people from the picker or People → New person.
- [x] **Faces** (2026-10-26): worker scans photos (OpenCV YuNet + SFace, on our server; models pinned by sha256 in the Dockerfile), groups similar faces, tags people from confirmed faces → mention statistics (lift, one-to-one) → nearest person. `/faces` "Who is this?" to name groups; person → "Tagged in N" to check; photo viewer shows boxes + per-face fixes; profile photo from any face crop (camera badge on the avatar). Auto-avatars for people without a photo.
- [x] **Organizations** (2026-10-26): `memberships` table (seeded from `Associated:`, re-import adds), colored tags on people rows and person header (add/remove), People filters (org popover, show, sort).
- [x] **Month view** `/calendar` (events, birthdays, todos due, days with entries); 5th tab.
- [x] **Graph** `/people/graph` (d3 force: org links + co-mention ties; focus by org/person).
- [x] **Contacts** `/people/contacts`: vCard import (match by phone, then unique name; unmatched reviewed) and export (per person "Save to Contacts", all as .vcf); Call/Message buttons.
- [x] **Face matching v2**: per-face match against each person's example faces (confirmed, else a group's core found via mentions), owner (you) held back until named, once per photo per person; "Is this X?" claim suggestions on every person's face page; "Someone else…" on a tagged face.
- [x] **Groups as directories** (2026-10-26): subgroups (`people.parent_id`), group page = subgroups + members (add/remove) + delete; a group in a day's text (`GH-19`, `@GH-19`, [[GH-19]]) or picked tags all members (`mentions.via_group`); taking one member out dissolves the group for that entry (`meta.dissolved_groups`), the rest stay tagged one by one.
- [x] **Meetings** per day (`mentions.meeting`, titles in `meta.meetings`); ties/often-with only within a meeting.
- [x] **@mentions** with autocomplete (`static/mention.js`), parsed server-side (`people.at_mentions`).
- [x] **Speed** (server stays in Sydney: Lightsail free plan can't change region): one DB round trip per page, preload on touch, service worker (`/sw.js`: pages stale-while-revalidate, immutable assets cache-first, POST clears pages), tab warm-up. Repeat/first taps ~20–180 ms instead of 0.5–1.3 s.
- [ ] Optional: Supabase project in Sydney (same region as the server) → server-side ~0.05 s, faster saves.

## People tagging v2 (2026-10-27)
- [x] **Met vs. mentioned** (`mentions.named_only`): met = @Name, a daily note's `People:` people, picked, or faces in the day's photos. Only named = `[[links]]`, names/groups written as plain text, a group in `People:`. Only met counts for last contact, "often with", graph ties and face evidence; a group only brings its members along when met (@Group or picked).
- [x] Plain-text names (`people.names_in_text`): full names, nicknames, and a unique Korean given name (김민수 → "민수랑") unless it's an everyday word (`COMMON_WORDS`). Two people with the same given name: not guessed.
- [x] **Photo tags** (`mentions.from_photo`, `journal.photo_mentions`): faces tagged in a day's photos tag the day (you excluded); 2+ of them sharing a group show under that group's 📷 label. ✕ = `meta.not_there`. Re-derived on save and after every face re-tag; never used as face evidence.
- [x] You (`settings.me`) are a member of every group, new ones too.
- [x] Day card: no "＋ Add" picker for the first meeting (@ in the text instead); extra meetings keep it. @ suggestions on phones: strip above the keyboard.
- [x] "Is this X?": only days you were with them; look-alikes of their tagged faces, then one face per look group seen 2x more around them; others' confirmed faces and yours left out. Works for you too.
- [x] Daily template boilerplate (« [[어제]] · [[내일]] », 지난 오늘:, `## 사건`) and empty headings removed from all notes (`python -m app.journal strip`, backup taken first); the importer strips them too.
