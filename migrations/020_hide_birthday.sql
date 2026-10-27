-- Someone whose birthday you don't want reminders for: off the People shelf and the month calendar
-- (still shown on their own page).
alter table people add column hide_birthday boolean not null default false;
