create extension if not exists pgcrypto;

create table if not exists public.waitlist_signups (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  email_normalized text generated always as (lower(btrim(email))) stored,
  source text not null default 'landing-page',
  status text not null default 'pending' check (status in ('pending', 'approved', 'invited', 'denied')),
  metadata jsonb not null default '{}'::jsonb,
  ip_hash text,
  created_at timestamptz not null default timezone('utc', now()),
  constraint waitlist_signups_email_format check (
    btrim(email) ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$'
  ),
  constraint waitlist_signups_email_normalized_unique unique (email_normalized)
);

alter table public.waitlist_signups
  add column if not exists ip_hash text;

alter table public.waitlist_signups
  drop constraint if exists waitlist_signups_email_format;

alter table public.waitlist_signups
  add constraint waitlist_signups_email_format check (
    btrim(email) ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$'
  ) not valid;

alter table public.waitlist_signups
  drop constraint if exists waitlist_signups_status_check;

alter table public.waitlist_signups
  add constraint waitlist_signups_status_check check (
    status in ('pending', 'approved', 'invited', 'denied')
  ) not valid;

create index if not exists waitlist_signups_created_at_idx
  on public.waitlist_signups (created_at desc);

create index if not exists waitlist_signups_status_created_at_idx
  on public.waitlist_signups (status, created_at desc);

create index if not exists waitlist_signups_ip_hash_created_at_idx
  on public.waitlist_signups (ip_hash, created_at desc)
  where ip_hash is not null;

alter table public.waitlist_signups enable row level security;

grant usage on schema public to anon, authenticated;
revoke all on table public.waitlist_signups from anon, authenticated;

drop policy if exists "public can insert waitlist signups" on public.waitlist_signups;
drop policy if exists "public can read waitlist signup count" on public.waitlist_signups;
