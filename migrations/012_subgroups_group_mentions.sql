-- Subgroups: GH → GH-19, GH-20… A group's members include its subgroups' members.
alter table people add column parent_id bigint references people (id) on delete set null;
alter table people add constraint people_parent_not_self check (parent_id <> id);
create index people_parent on people (parent_id) where parent_id is not null;

-- Seeded from the Obsidian `Subgroups:` links of group notes.
update people c set parent_id = g.id
from people g
cross join lateral jsonb_array_elements_text(
    case jsonb_typeof(g.fields -> 'Subgroups') when 'array' then g.fields -> 'Subgroups' else '[]'::jsonb end) a
where g.kind = 'group' and c.kind = 'group' and c.id <> g.id and c.parent_id is null
  and regexp_replace(a, '^\[\[([^\]|#]+).*$', '\1') in (select unnest(array[c.name] || c.aliases));

-- A group mentioned in an entry tags its members "under the hood": their mention rows point at
-- the group they came from. (Picked or [[linked]] people have via_group null.)
alter table mentions add column via_group bigint references people (id) on delete set null;
