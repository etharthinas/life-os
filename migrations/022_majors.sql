-- Someone can have several majors (복수전공, 부전공): `major` becomes a list.
alter table people add column majors text[] not null default '{}';
update people set majors = array[major] where major is not null and major <> '';
alter table people drop column major;
