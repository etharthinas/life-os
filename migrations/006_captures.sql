-- Scratchpad: free text that Claude sorts into todos. Each capture remembers what it added, so
-- the whole batch can be undone.
create table captures (
    id         bigint generated always as identity primary key,
    body       text not null,
    source     text not null,
    default_due date,                        -- written on a day page: that day, unless the text says otherwise
    status     text not null default 'pending' check (status in ('pending', 'done', 'fallback', 'undone')),
    note       text,                         -- Claude's question about anything it couldn't decide
    created_at timestamptz not null default now(),
    done_at    timestamptz
);
create index captures_pending on captures (created_at) where status = 'pending';
alter table captures enable row level security;

alter table todos add column capture_id bigint references captures on delete set null;
alter table todos add column notes text;
