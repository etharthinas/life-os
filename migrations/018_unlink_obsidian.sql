-- No more Obsidian: the person/group notes ("Original note") go, and so do the raw note fields on
-- people. Everything still needed already lives in its own columns (school, major, 학번, birthday,
-- nicknames, memberships); phone numbers move to the contact card here. Daily journals stay, as
-- ordinary journals of this app; knowledge notes (concepts, frameworks, …) stay as notes.

-- Phone numbers from the note, for people whose contact card has none: `tel:010…`, `[010…](tel:…)`.
update people p set contact = coalesce(p.contact, '{}'::jsonb) || jsonb_build_object('phones', x.phones)
from (
    select id, jsonb_agg(distinct trim(regexp_replace(regexp_replace(v, '^\[([^\]]+)\]\(tel:.*$', '\1'), '^tel:', '', 'i'))) as phones
    from people, jsonb_array_elements_text(case jsonb_typeof(fields -> 'Phone') when 'array' then fields -> 'Phone'
                                             when 'string' then jsonb_build_array(fields -> 'Phone') else '[]'::jsonb end) v
    where trim(v) <> ''
    group by id
) x
where x.id = p.id and coalesce(jsonb_array_length(p.contact -> 'phones'), 0) = 0;

create temporary table entity_notes on commit drop as
select raw_id as id from people where raw_id is not null
union select id from raw_entries where source = 'obsidian' and meta ->> 'folder' = 'Entities';
update people set raw_id = null where raw_id is not null;
delete from raw_entries where id in (select id from entity_notes);
drop index if exists people_raw_id;
alter table people drop column raw_id, drop column fields;

update raw_entries set source = 'web', kind = 'journal', meta = meta - 'path' - 'folder' - 'frontmatter'
where source = 'obsidian' and meta ->> 'type' = 'daily';

-- The other imported notes are just notes now.
update raw_entries set source = 'note', kind = 'note', meta = meta - 'path' - 'folder' where source = 'obsidian';
