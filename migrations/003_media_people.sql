-- Images. The bytes live in object storage under keys derived from the hash
-- (orig/<sha256>, display/<sha256>.webp, thumb/<sha256>.webp), so the DB only holds this row.
create table media (
    id           bigint generated always as identity primary key,
    sha256       text not null unique,
    name         text,                          -- original file name (Obsidian embeds resolve by it)
    content_type text not null,
    bytes        bigint not null,
    width        integer,
    height       integer,
    taken_at     timestamptz,                   -- EXIF DateTimeOriginal
    source       text not null,                 -- obsidian | web | telegram
    created_at   timestamptz not null default now()
);
create index media_name on media (name);

-- Images attached to a raw entry (Obsidian embeds, photos added to a log), in order.
create table raw_media (
    raw_id   bigint not null references raw_entries (id) on delete cascade,
    media_id bigint not null references media (id) on delete cascade,
    pos      smallint not null default 0,
    primary key (raw_id, media_id)
);
create index raw_media_media on raw_media (media_id);

-- Editable person data (the imported frontmatter in people.fields stays as it was).
alter table people
    add column photo_id           bigint references media (id) on delete set null,
    add column keep_in_touch_days integer check (keep_in_touch_days > 0),
    add column gift_ideas         text[] not null default '{}';

alter table media enable row level security;
alter table raw_media enable row level security;
