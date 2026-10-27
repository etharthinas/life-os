-- Organizations (groups) people belong to: seeded from the Obsidian `Associated:` links, editable here.
create table memberships (
    person_id bigint not null references people (id) on delete cascade,
    group_id  bigint not null references people (id) on delete cascade,
    primary key (person_id, group_id),
    check (person_id <> group_id)
);
create index memberships_group on memberships (group_id);
alter table memberships enable row level security;

insert into memberships (person_id, group_id)
select distinct p.id, g.id
from people p
cross join lateral jsonb_array_elements_text(
    case jsonb_typeof(p.fields -> 'Associated')
        when 'array' then p.fields -> 'Associated'
        when 'string' then jsonb_build_array(p.fields -> 'Associated')
        else '[]'::jsonb end) a
join people g on g.kind = 'group' and g.id <> p.id
 and regexp_replace(a, '^\[\[([^\]|#]+).*$', '\1') in (select unnest(array[g.name] || g.aliases))
on conflict do nothing;

-- Phone-contacts data (from a vCard import, or typed in): {"phones": [...], "emails": [...], "org": ..., "uid": ...}
alter table people add column contact jsonb not null default '{}';
