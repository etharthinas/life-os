-- People are identified by id only: two people can share a name. An Obsidian Entities note is
-- still one person (unique raw_id), so a re-import updates instead of duplicating.
alter table people drop constraint people_name_key;
create index people_name on people (name);
create unique index people_raw_id on people (raw_id) where raw_id is not null;
alter table people add column about text;  -- a few words that tell same-named people apart

-- Who was there, picked in the people picker (as opposed to found as a [[link]] in the text).
-- Re-deriving mentions from the text leaves picked ones alone.
alter table mentions add column picked boolean not null default false;
