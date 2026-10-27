-- A group you were with as a whole now follows the group (journal.follow_whole_groups); one you took
-- someone out of is those people instead (journal._freeze_groups_of). Days trimmed before this:
-- the group's tag becomes a label over the people it brought, who are tagged one by one.
create temporary table trimmed on commit drop as
with recursive tree(root, id) as (
    select id, id from people where kind = 'group'
    union select t.root, c.id from people c join tree t on c.parent_id = t.id
)
select distinct m.raw_id, m.person_id as g
from mentions m
join people gp on gp.id = m.person_id and gp.kind = 'group'
join raw_entries r on r.id = m.raw_id and r.meta ->> 'type' = 'daily'
where not m.auto_group and not m.named_only
  and exists (select 1 from jsonb_array_elements_text(coalesce(r.meta -> 'not_there', '[]')) x
              join tree t on t.root = m.person_id join memberships ms on ms.group_id = t.id
              where ms.person_id = x::bigint);

update mentions m set via_group = null, picked = true from trimmed t where m.raw_id = t.raw_id and m.via_group = t.g;
update mentions m set auto_group = true, picked = true from trimmed t where m.raw_id = t.raw_id and m.person_id = t.g;

-- And once now: whole groups take in members who joined since (the worker runs it).
insert into settings (key, value) values ('groups_dirty', 'true') on conflict (key) do nothing;
