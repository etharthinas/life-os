-- macOS file names are NFD (decomposed Hangul) while note text is NFC, so the Obsidian import created
-- each Korean person twice: one row from the Entities/ file name (birthday, phone, no mentions) and
-- one from daily `People:` lists (all the mentions). Merge them into the row with the note, in NFC.
create temp table person_merge on commit drop as
select p.id as from_id, k.id as to_id
from people p
cross join lateral (
    select q.id from people q where normalize(q.name, nfc) = normalize(p.name, nfc)
    order by q.raw_id is null, q.id limit 1) k
where k.id <> p.id;

insert into mentions (raw_id, person_id)
select m.raw_id, pm.to_id from mentions m join person_merge pm on pm.from_id = m.person_id
on conflict do nothing;

update people k set
    aliases = coalesce((select array_agg(distinct a) from (
        select unnest(k.aliases) a
        union select unnest(p.aliases) from person_merge pm join people p on p.id = pm.from_id where pm.to_id = k.id
    ) x where a <> ''), '{}'),
    photo_id = coalesce(k.photo_id, (select p.photo_id from person_merge pm join people p on p.id = pm.from_id
                                     where pm.to_id = k.id and p.photo_id is not null limit 1))
where k.id in (select to_id from person_merge);

delete from people where id in (select from_id from person_merge);

update people set name = normalize(name, nfc) where name is not nfc normalized;
update people set aliases = (select coalesce(array_agg(distinct normalize(a, nfc)), '{}') from unnest(aliases) a)
where exists (select 1 from unnest(aliases) a where a is not nfc normalized);
update raw_entries set meta = jsonb_set(meta, '{title}', to_jsonb(normalize(meta ->> 'title', nfc)))
where source = 'obsidian' and meta ->> 'title' is not nfc normalized;
update media set name = normalize(name, nfc) where name is not nfc normalized;
