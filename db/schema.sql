-- DrakonRhymServer — Supabase schema.
--
-- Run this once in Supabase → SQL Editor for a fresh project.
-- Re-runnable: every statement uses IF NOT EXISTS / CREATE OR REPLACE.

create extension if not exists "pgcrypto";

-- ---------- users ----------
create table if not exists public.users (
  id                    uuid primary key default gen_random_uuid(),
  google_id             text not null unique,
  email                 text,
  name                  text,
  avatar_url            text,
  created_at            timestamptz not null default now(),
  last_login_at         timestamptz not null default now(),
  download_count        integer not null default 0,
  daily_download_limit  integer not null default 5,
  daily_download_used   integer not null default 0,
  last_download_date    date
);

create index if not exists users_google_id_idx on public.users (google_id);

-- ---------- downloads ----------
create table if not exists public.downloads (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references public.users(id) on delete cascade,
  youtube_url    text not null,
  semitones      integer not null default 0,
  cents          integer not null default 0,
  downloaded_at  timestamptz not null default now(),
  status         text not null check (status in ('success', 'failed'))
);

create index if not exists downloads_user_id_idx on public.downloads (user_id, downloaded_at desc);

-- ---------- RPC: consume_quota ----------
-- Atomically: roll over the daily counter if it's a new day, check the
-- per-user limit, increment used/download_count, and return the new state.
-- Returns { allowed: bool, used: int, "limit": int }.
create or replace function public.consume_quota(p_user_id uuid)
returns jsonb
language plpgsql
as $$
declare
  v_user public.users%rowtype;
  v_today date := current_date;
  v_used  integer;
begin
  select * into v_user from public.users where id = p_user_id for update;
  if not found then
    return jsonb_build_object('allowed', false, 'used', 0, 'limit', 0, 'error', 'user_not_found');
  end if;

  -- New day → reset
  if v_user.last_download_date is null or v_user.last_download_date < v_today then
    v_used := 0;
  else
    v_used := v_user.daily_download_used;
  end if;

  if v_used >= v_user.daily_download_limit then
    -- Still persist the rollover so a no-op call doesn't keep yesterday's used.
    update public.users
       set daily_download_used = v_used,
           last_download_date  = coalesce(v_user.last_download_date, v_today)
     where id = p_user_id;
    return jsonb_build_object('allowed', false, 'used', v_used, 'limit', v_user.daily_download_limit);
  end if;

  update public.users
     set daily_download_used = v_used + 1,
         download_count      = v_user.download_count + 1,
         last_download_date  = v_today
   where id = p_user_id;

  return jsonb_build_object('allowed', true, 'used', v_used + 1, 'limit', v_user.daily_download_limit);
end;
$$;

-- ---------- RPC: refund_quota ----------
-- Roll back a previously consumed slot (called when a download fails after
-- consume succeeded). Floors at 0 so a stuck call can't underflow.
create or replace function public.refund_quota(p_user_id uuid)
returns void
language plpgsql
as $$
begin
  update public.users
     set daily_download_used = greatest(daily_download_used - 1, 0),
         download_count      = greatest(download_count - 1, 0)
   where id = p_user_id;
end;
$$;
