-- Key/value settings (block target calendar, day window, ...).
create table settings (
    key   text primary key,
    value jsonb not null
);
insert into settings (key, value) values
    ('day_start', '"09:00"'),
    ('day_end', '"22:00"'),
    ('default_estimate_min', '30');

-- Google Calendar. Blocks are written into my existing calendars, tagged with
-- extendedProperties.private.lifeos_block so the app only ever touches its own events.
create table google_accounts (
    email         text primary key,
    refresh_token text not null,
    created_at    timestamptz not null default now()
);

create table calendars (
    id          text primary key,              -- Google calendar id
    account     text not null references google_accounts (email) on delete cascade,
    summary     text not null,
    color       text,
    access_role text not null,                 -- owner | writer | reader | freeBusyReader
    is_primary  boolean not null default false,
    synced      boolean not null default true  -- include in sync + busy time
);

create table events (
    calendar_id  text not null references calendars (id) on delete cascade,
    event_id     text not null,
    summary      text,
    start_at     timestamptz not null,
    end_at       timestamptz not null,
    all_day      boolean not null default false,
    transparent  boolean not null default false,  -- "show as free"
    lifeos_block bigint,                          -- set when the app created this event
    html_link    text,
    primary key (calendar_id, event_id)
);
create index events_start on events (start_at);

create table blocks (
    id          bigint generated always as identity primary key,
    todo_id     bigint not null references todos (id) on delete cascade,
    plan_date   date not null,
    start_at    timestamptz not null,
    end_at      timestamptz not null,
    status      text not null default 'proposed' check (status in ('proposed', 'accepted', 'dismissed')),
    calendar_id text,
    event_id    text,
    created_at  timestamptz not null default now()
);
create index blocks_plan_date on blocks (plan_date, status);

-- Obsidian import: one raw entry per file, keyed by its vault path.
create unique index raw_entries_obsidian_path on raw_entries ((meta ->> 'path')) where source = 'obsidian';

create extension if not exists pg_trgm;
create index raw_entries_body_trgm on raw_entries using gin (body gin_trgm_ops);

-- People & groups (seeded from Obsidian Entities/).
create table people (
    id          bigint generated always as identity primary key,
    name        text not null unique,
    kind        text not null default 'person' check (kind in ('person', 'group')),
    aliases     text[] not null default '{}',
    birth_year  smallint,
    birth_month smallint check (birth_month between 1 and 12),
    birth_day   smallint check (birth_day between 1 and 31),
    fields      jsonb not null default '{}',    -- remaining frontmatter (Major, Phone, ...)
    raw_id      bigint references raw_entries (id),
    created_at  timestamptz not null default now()
);

create table mentions (
    raw_id    bigint not null references raw_entries (id) on delete cascade,
    person_id bigint not null references people (id) on delete cascade,
    primary key (raw_id, person_id)
);
create index mentions_person on mentions (person_id);

alter table settings enable row level security;
alter table google_accounts enable row level security;
alter table calendars enable row level security;
alter table events enable row level security;
alter table blocks enable row level security;
alter table people enable row level security;
alter table mentions enable row level security;
