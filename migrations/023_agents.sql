-- Agents tab: project channels, Claude Code agents in them, task threads.
create table agent_channels (
  id bigint generated always as identity primary key,
  name text not null unique,
  repo_url text,                      -- null: no repo (research only)
  default_branch text not null default 'main',
  created_at timestamptz not null default now()
);

create table agents (
  id bigint generated always as identity primary key,
  channel_id bigint not null references agent_channels on delete cascade,
  name text not null,
  kind text not null check (kind in ('code', 'research')),
  model text not null default 'sonnet',
  role text not null default '',
  created_at timestamptz not null default now(),
  unique (channel_id, name)
);

create table agent_tasks (
  id bigint generated always as identity primary key,
  agent_id bigint not null references agents on delete cascade,
  title text not null,
  status text not null default 'queued'
    check (status in ('queued', 'running', 'done', 'failed', 'cancelled')),
  session_id text,
  pr_url text,
  error text,
  queued_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  seen_at timestamptz
);
create index agent_tasks_queue on agent_tasks (queued_at) where status = 'queued';

create table agent_messages (
  id bigint generated always as identity primary key,
  task_id bigint not null references agent_tasks on delete cascade,
  author text not null check (author in ('me', 'agent', 'system')),
  body text not null,
  created_at timestamptz not null default now()
);
create index agent_messages_task on agent_messages (task_id, id);

-- The agents container connects as a role that can only touch these tables. Run once, by hand,
-- as the owner (Supabase SQL editor), with a real password:
--   create role agents_runner login password '...';
--   grant usage on schema public to agents_runner;
--   grant select on agent_channels, agents to agents_runner;
--   grant select, insert, update on agent_tasks, agent_messages to agents_runner;
-- Supabase session pooler user name: agents_runner.<project ref>
