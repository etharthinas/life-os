-- Phone numbers on contact cards as 010-1234-5678 (contacts.format_phone): Korean mobile numbers
-- in any format (+82 10…, 010.1234.5678, 01012345678, 1012345678) are rewritten; anything else is left as is.
create function pg_temp.fmt_phone(v text) returns text language sql immutable as $$
    select case
        when d ~ '^01[0-9]{9}$' then substr(d, 1, 3) || '-' || substr(d, 4, 4) || '-' || substr(d, 8)
        when d ~ '^01[0-9]{8}$' then substr(d, 1, 3) || '-' || substr(d, 4, 3) || '-' || substr(d, 7)
        else v end
    from (select case when x ~ '^82' and length(x) >= 11 then '0' || substr(x, 3)
                      when x ~ '^1[016789][0-9]{7,8}$' then '0' || x else x end as d
          from (select regexp_replace(v, '[^0-9]', '', 'g') as x) a) b
$$;

update people p set contact = jsonb_set(p.contact, '{phones}', x.phones)
from (
    select id, jsonb_agg(pg_temp.fmt_phone(v) order by n) as phones
    from people, jsonb_array_elements_text(contact -> 'phones') with ordinality as t(v, n)
    where jsonb_typeof(contact -> 'phones') = 'array'
    group by id
) x
where x.id = p.id and x.phones is distinct from p.contact -> 'phones';
