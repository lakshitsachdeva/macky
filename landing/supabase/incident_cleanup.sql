-- Incident cleanup playbook for spammed waitlist / whitelist tables.
-- Review each query result before running the DELETE/TRUNCATE statements.

begin;

-- 1) Backup current data before touching anything.
create table if not exists public.waitlist_signups_backup_20260315 as
select *
from public.waitlist_signups;

create table if not exists public.whitelist_backup_20260315 as
select *
from public.whitelist;

commit;

-- 2) Inspect when the spam landed.
select count(*) as total_rows,
       min(created_at) as first_row_at,
       max(created_at) as last_row_at
from public.waitlist_signups;

select date_trunc('minute', created_at) as minute_bucket,
       count(*) as rows_added
from public.waitlist_signups
group by 1
order by 1 desc;

select lower(split_part(email, '@', 2)) as domain,
       count(*) as rows_added
from public.waitlist_signups
group by 1
order by 2 desc
limit 100;

-- 3) Check whether your old legit rows are still there.
select count(*) as rows_before_attack
from public.waitlist_signups
where created_at < timestamptz '2026-03-14 17:40:00+00';

select *
from public.waitlist_signups
order by created_at asc
limit 100;

-- 4) If whitelist should be empty right now, wipe it.
-- truncate table public.whitelist;

-- 5) Safer waitlist cleanup option:
-- delete spam only from the known attack window.
-- Review the timestamp window first.
-- delete from public.waitlist_signups
-- where created_at >= timestamptz '2026-03-14 17:40:00+00';

-- 6) Even safer option:
-- delete by attack window + suspicious domains.
-- Replace the domain list with the ones returned above.
-- delete from public.waitlist_signups
-- where created_at >= timestamptz '2026-03-14 17:40:00+00'
--   and lower(split_part(email, '@', 2)) in (
--     'evil.com',
--     'raycastisbetteryoumf.ru'
--   );

-- 6b) Target obvious injection/XSS payload rows directly.
-- Review first:
-- select *
-- from public.waitlist_signups
-- where email ~* '(<script|onerror=|<img|''\s+or\s+1=1|--$)';
--
-- Then delete:
-- delete from public.waitlist_signups
-- where email ~* '(<script|onerror=|<img|''\s+or\s+1=1|--$)';

-- 7) Find all rows that violate the stricter email rule.
-- select id, email, created_at
-- from public.waitlist_signups
-- where not (btrim(email) ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$')
-- order by created_at desc;
--
-- delete from public.waitlist_signups
-- where not (btrim(email) ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$');
--
-- After cleanup, validate the constraint:
-- alter table public.waitlist_signups validate constraint waitlist_signups_email_format;
-- alter table public.whitelist validate constraint whitelist_email_format;

-- 8) If you accidentally delete too much, restore from backup.
-- insert into public.waitlist_signups
-- select *
-- from public.waitlist_signups_backup_20260315;
