-- Hardens admin-only Supabase tables and RPCs so browsers cannot
-- write to them directly through anon/authenticated roles.

begin;

grant usage on schema public to anon, authenticated;

revoke all on all sequences in schema public from anon, authenticated;
grant usage, select on all sequences in schema public to service_role;

alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;

do $$
declare
  target_table text;
  policy_record record;
  function_record record;
begin
  foreach target_table in array array['access', 'profiles', 'usage', 'whitelist', 'waitlist_signups']
  loop
    if exists (
      select 1
      from pg_tables
      where schemaname = 'public' and tablename = target_table
    ) then
      execute format('alter table public.%I enable row level security', target_table);
      execute format('revoke all on table public.%I from anon, authenticated', target_table);
      execute format(
        'grant select, insert, update, delete, truncate, references, trigger on table public.%I to service_role',
        target_table
      );

      for policy_record in
        select policyname
        from pg_policies
        where schemaname = 'public' and tablename = target_table
      loop
        execute format('drop policy if exists %I on public.%I', policy_record.policyname, target_table);
      end loop;
    end if;
  end loop;

  for function_record in
    select p.oid::regprocedure as signature
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('check_access', 'increment_search_count')
  loop
    execute format('revoke all on function %s from public, anon, authenticated', function_record.signature);
    execute format('grant execute on function %s to service_role', function_record.signature);
  end loop;
end
$$;

do $$
begin
  if exists (
    select 1
    from pg_tables
    where schemaname = 'public' and tablename = 'whitelist'
  ) and exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'whitelist'
      and column_name = 'email'
  ) and not exists (
    select 1
    from pg_indexes
    where schemaname = 'public'
      and indexname = 'whitelist_email_normalized_unique_idx'
  ) then
    if exists (
      select 1
      from public.whitelist
      group by lower(btrim(email))
      having count(*) > 1
    ) then
      raise notice 'Skipping whitelist email unique index because duplicate emails already exist. Clean the table and rerun the create-index statement.';
    else
      execute 'create unique index whitelist_email_normalized_unique_idx on public.whitelist ((lower(btrim(email))))';
    end if;
  end if;
end
$$;

do $$
begin
  if exists (
    select 1
    from pg_tables
    where schemaname = 'public' and tablename = 'whitelist'
  ) and exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'whitelist'
      and column_name = 'email'
  ) then
    execute 'alter table public.whitelist drop constraint if exists whitelist_email_format';
    execute $sql$
      alter table public.whitelist
      add constraint whitelist_email_format
      check (btrim(email) ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$')
      not valid
    $sql$;
  end if;
end
$$;

commit;
