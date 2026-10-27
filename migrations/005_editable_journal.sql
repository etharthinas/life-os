-- Journals are editable and the latest text wins (decided 2026-10-26); recovery is the nightly backup.
-- The LLM still never writes to raw_entries; only the person does.
drop trigger raw_entries_immutable on raw_entries;
drop function raw_entries_immutable();
alter table raw_entries add column updated_at timestamptz;

-- 'journal': a day's document written in the app (imported ones stay 'import' with meta.type = daily).
alter table raw_entries drop constraint raw_entries_kind_check;
alter table raw_entries add constraint raw_entries_kind_check check (kind in ('log', 'note', 'import', 'journal'));
