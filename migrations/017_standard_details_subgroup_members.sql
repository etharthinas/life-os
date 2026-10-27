-- Every person has the same details: school, major, 학번 (two digits, 21 = 2021), next to the
-- birthday, phone/email (contact) and nicknames already in their own columns. Filled once from the
-- Obsidian note's fields ("고려대-산업공학과" → 고려대 / 산업공학과; no school → 서울대; "Class of: 21").
-- The note's `fields` stay as imported; the app edits the columns (people.STANDARDIZE_SQL repeats this
-- after a re-import, filling only what's empty).
alter table people add column school text, add column major text, add column class_of smallint;

update people p set
    school = coalesce(p.school, case when x.m like '%-%' then trim(split_part(x.m, '-', 1)) else '서울대' end),
    major  = coalesce(p.major, case when x.m like '%-%' then trim(substr(x.m, strpos(x.m, '-') + 1)) else x.m end)
from (select id, nullif(trim(case jsonb_typeof(fields -> 'Major') when 'array' then fields -> 'Major' ->> 0
                                                                  when 'string' then fields ->> 'Major' end), '') as m
      from people) x
where x.id = p.id and x.m is not null and p.kind = 'person';

update people set class_of = coalesce(class_of, (
    select case when n >= 100 then n % 100 else n end
    from (select nullif(regexp_replace(fields ->> 'Class of', '\D', '', 'g'), '')::int as n) y))
where kind = 'person' and fields ? 'Class of';

-- A group inside a group: the outer one has everyone in the inner ones too (GH has GH-19's members).
insert into memberships (person_id, group_id)
with recursive up(group_id, ancestor) as (
    select id, parent_id from people where kind = 'group' and parent_id is not null
    union
    select up.group_id, g.parent_id from up join people g on g.id = up.ancestor where g.parent_id is not null
)
select distinct ms.person_id, up.ancestor from memberships ms join up on up.group_id = ms.group_id
where ms.person_id <> up.ancestor
on conflict do nothing;

insert into settings (key, value) values ('groups_dirty', 'true') on conflict (key) do nothing;

-- Groups don't have a person's details.
update people set birth_year = null, birth_month = null, birth_day = null, keep_in_touch_days = null,
                  school = null, major = null, class_of = null
where kind = 'group';
