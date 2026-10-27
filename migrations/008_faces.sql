-- Faces found in photos (OpenCV YuNet + SFace, run on our own server), and who they are.
create table faces (
    id         bigint generated always as identity primary key,
    media_id   bigint not null references media (id) on delete cascade,
    x real not null, y real not null, w real not null, h real not null,  -- fractions of the image size
    embedding  real[] not null,                                           -- SFace, 128-d, unit length
    cluster    integer,                                                   -- similar faces, from the last clustering
    person_id  bigint references people (id) on delete set null,
    -- auto: tagged by matching; confirmed: by you; rejected: person_id is who it is NOT;
    -- ignored: not someone to tag (a stranger, a poster); null: not known yet
    status     text check (status in ('auto', 'confirmed', 'rejected', 'ignored')),
    created_at timestamptz not null default now()
);
create index faces_media on faces (media_id);
create index faces_person on faces (person_id) where status in ('auto', 'confirmed');
alter table faces enable row level security;

alter table media add column faces_scanned boolean not null default false;
