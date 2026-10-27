-- Groups labelled automatically: people at a meeting who are all in a group show under it (any way
-- they were tagged, not just photos). See journal.auto_groups; the worker's re-tag fills them in.
alter table mentions add column auto_group boolean not null default false;
delete from mentions m using people g where g.id = m.person_id and g.kind = 'group' and m.from_photo and not m.picked;

-- You're never tagged in your own entries.
delete from mentions where person_id = (select (value ->> 'person_id')::bigint from settings where key = 'me');

insert into settings (key, value) values ('faces_dirty', 'true') on conflict (key) do nothing;
