-- People whose faces are tagged in a day's photos are tagged in that day's journal too
-- (`journal.photo_mentions`). They're re-derived after every save and face re-tag, and never count
-- as evidence for face matching (that would feed on itself).
alter table mentions add column from_photo boolean not null default false;

-- Named in the text ([[Name]], a group written as plain text) but not met: the entry is about them,
-- yet it's no meeting (no last contact, not "together", no group members brought along). Met =
-- @Name, a daily note's `People:` list, picked, or seen in the photos. Existing rows are worked
-- out from the text once; every save re-derives them (journal.sync).
alter table mentions add column named_only boolean not null default false;

update mentions m set named_only = true
from raw_entries r, people p
where r.id = m.raw_id and p.id = m.person_id and not m.picked and m.via_group is null
  and not exists (
    select 1 from unnest(array[p.name] || p.aliases) n
    where position('@' || n in r.body) > 0
       or exists (select 1 from jsonb_array_elements_text(case jsonb_typeof(r.meta -> 'frontmatter' -> 'People')
                                 when 'array' then r.meta -> 'frontmatter' -> 'People' else '[]'::jsonb end) e
                  where normalize(e, NFC) like '[[' || n || ']]%' or normalize(e, NFC) like '[[' || n || '|%'));

-- So is a group in `People:` (some of them were there, not all).
update mentions m set named_only = true from people g
where g.id = m.person_id and g.kind = 'group' and not m.picked and m.via_group is null
  and not exists (select 1 from raw_entries r, unnest(array[g.name] || g.aliases) n
                  where r.id = m.raw_id and position('@' || n in r.body) > 0);

-- A group only named brings no one along.
delete from mentions v using mentions g
where v.raw_id = g.raw_id and v.via_group = g.person_id and g.named_only;

-- You're in every group: the diary's owner belongs to all of them (new groups too, see people.create).
insert into memberships (person_id, group_id)
select (s.value ->> 'person_id')::bigint, g.id
from settings s join people g on g.kind = 'group'
where s.key = 'me' and (s.value ->> 'person_id')::bigint <> g.id
  and exists (select 1 from people where id = (s.value ->> 'person_id')::bigint)
on conflict do nothing;

-- The worker's next face re-tag fills in the photo tags.
insert into settings (key, value) values ('faces_dirty', 'true') on conflict (key) do nothing;
