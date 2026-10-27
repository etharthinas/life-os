-- Redo face matching with the per-face method (faces.relabel): drop every automatic result (tags,
-- groups, auto-picked profile photos) and let the worker recompute. Your confirmations, "not them"
-- corrections and hand-picked profile photos stay.
update faces set status = null, person_id = null where status = 'auto';
update faces set cluster = null;
update people set photo_id = null
where photo_id in (select id from media where source = 'avatar' and (name is null or name like 'auto-face-%'));
insert into settings (key, value) values ('faces_dirty', 'true') on conflict (key) do nothing;
