-- Several meetings on one day: who was at which (0 = the first, or the only one). Titles live in
-- the day's journal meta as "meetings": ["", "IVF 저녁", ...]. People only count as "together"
-- (often with, graph ties) within the same meeting.
alter table mentions add column meeting smallint not null default 0;
