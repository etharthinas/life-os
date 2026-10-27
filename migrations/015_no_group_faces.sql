-- Only people have faces: tags on a group (picked by mistake) go back to unknown.
update faces f set person_id = null, status = null
from people g where g.id = f.person_id and g.kind = 'group';
delete from face_rejections r using people g where g.id = r.person_id and g.kind = 'group';
insert into settings (key, value) values ('faces_dirty', 'true') on conflict (key) do nothing;
