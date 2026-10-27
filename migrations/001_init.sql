-- Layer 1: raw human input. Append-only: the body is never rewritten (by the LLM or anything else).
create table raw_entries (
    id          bigint generated always as identity primary key,
    kind        text not null check (kind in ('log', 'note', 'import')),
    source      text not null,                 -- web | telegram | obsidian | ...
    body        text not null,
    occurred_at timestamptz not null default now(),
    created_at  timestamptz not null default now(),
    meta        jsonb not null default '{}'
);
create index raw_entries_occurred_at on raw_entries (occurred_at desc);

create function raw_entries_immutable() returns trigger language plpgsql as $$
begin
    if new.body is distinct from old.body or new.occurred_at is distinct from old.occurred_at then
        raise exception 'raw_entries.body/occurred_at are immutable (id=%)', old.id;
    end if;
    return new;
end $$;
create trigger raw_entries_immutable before update on raw_entries
    for each row execute function raw_entries_immutable();

create table todos (
    id           bigint generated always as identity primary key,
    title        text not null,
    status       text not null default 'open' check (status in ('open', 'done', 'dropped')),
    priority     smallint not null default 2 check (priority between 1 and 3),  -- 1 = high
    due_on       date,
    estimate_min integer,
    project      text,
    source       text not null,
    source_ref   text,                          -- e.g. Notion page id, for dedupe
    created_at   timestamptz not null default now(),
    done_at      timestamptz
);
create index todos_open on todos (status, due_on);
create unique index todos_source_ref on todos (source, source_ref) where source_ref is not null;

-- Supabase exposes the public schema over its REST API with the anon key.
-- The app connects as the postgres role (bypasses RLS), so RLS with no policies = no public access.
alter table raw_entries enable row level security;
alter table todos enable row level security;
