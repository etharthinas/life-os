-- "Not this person" is per person: a face rejected for one person can still be matched to another.
create table face_rejections (
    face_id   bigint not null references faces (id) on delete cascade,
    person_id bigint not null references people (id) on delete cascade,
    primary key (face_id, person_id)
);
alter table face_rejections enable row level security;

insert into face_rejections (face_id, person_id)
select id, person_id from faces where status = 'rejected' and person_id is not null;
update faces set status = null, person_id = null where status = 'rejected';
alter table faces drop constraint faces_status_check;
alter table faces add constraint faces_status_check check (status in ('auto', 'confirmed', 'ignored'));
