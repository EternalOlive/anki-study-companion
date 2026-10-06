-- Migration: 20261006_member_pokes.sql
-- Room members can "poke" (찌르기) another member of the same room.
--
-- 1. member_pokes: one row per poke. Clients never touch the table directly;
--    RLS is enabled and every privilege is revoked, so all access goes through
--    the two SECURITY DEFINER RPCs below.
-- 2. poke_room_member(target_group, target_user) -> created_at
--    Caller and target must both be current members of the room (bans remove
--    membership, and a ban row is checked explicitly as well). Self-pokes are
--    rejected. Rate limits: the same sender -> receiver pair in the same room
--    at most once per 60 seconds ('poke too soon'), and at most 30 pokes per
--    sender per hour across all rooms ('too many pokes'). Pokes older than
--    7 days are deleted opportunistically on each call.
-- 3. fetch_my_pokes(target_group, since) -> (id, from_user, created_at)
--    Returns unseen pokes addressed to the caller in that room that are newer
--    than `since` (default: the last 10 minutes), oldest first, at most 50,
--    and marks exactly those rows seen in the same statement. Delivery is
--    therefore at-most-once per poke: whichever client of the account fetches
--    first consumes it, and a retried call never shows the same poke twice.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

create table if not exists public.member_pokes (
  id bigint generated always as identity primary key,
  group_id uuid not null references public.study_groups(id) on delete cascade,
  from_user uuid not null references auth.users(id) on delete cascade,
  to_user uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  seen_at timestamptz,
  constraint member_pokes_not_self check (from_user <> to_user)
);

-- Receiver inbox (fetch_my_pokes).
create index if not exists member_pokes_to_user_created_at_idx
  on public.member_pokes (to_user, created_at desc);
-- Sender rate limits (poke_room_member).
create index if not exists member_pokes_from_user_created_at_idx
  on public.member_pokes (from_user, created_at desc);
-- 7-day clean-up.
create index if not exists member_pokes_created_at_idx
  on public.member_pokes (created_at);

alter table public.member_pokes enable row level security;
revoke all on public.member_pokes from public, anon, authenticated;

create or replace function public.poke_room_member(
  target_group uuid,
  target_user uuid
)
returns timestamptz
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  actor uuid := auth.uid();
  created timestamptz;
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_group is null or target_user is null then
    raise exception 'group and target user are required';
  end if;
  if target_user = actor then
    raise exception 'cannot poke yourself';
  end if;

  if not exists (
    select 1
    from public.group_members as members
    where members.group_id = target_group
      and members.user_id = actor
  ) then
    raise exception 'not a member of this group';
  end if;

  -- Membership already excludes banned accounts (moderation deletes the
  -- membership and the insert trigger refuses re-entry). Check the ban table
  -- too so a stale membership row can never be used to poke.
  if exists (
    select 1
    from public.study_group_bans as bans
    where bans.group_id = target_group
      and bans.user_id = actor
  ) then
    raise exception 'blocked from this group';
  end if;

  if not exists (
    select 1
    from public.group_members as members
    where members.group_id = target_group
      and members.user_id = target_user
  ) or exists (
    select 1
    from public.study_group_bans as bans
    where bans.group_id = target_group
      and bans.user_id = target_user
  ) then
    raise exception 'target is not in this group';
  end if;

  -- Serialize one sender's pokes so concurrent calls cannot both pass the
  -- rate-limit checks below.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('member_pokes:' || actor::text, 0)
  );

  if exists (
    select 1
    from public.member_pokes as pokes
    where pokes.from_user = actor
      and pokes.to_user = target_user
      and pokes.group_id = target_group
      and pokes.created_at > now() - interval '60 seconds'
  ) then
    raise exception 'poke too soon';
  end if;

  if (
    select count(*)
    from public.member_pokes as pokes
    where pokes.from_user = actor
      and pokes.created_at > now() - interval '1 hour'
  ) >= 30 then
    raise exception 'too many pokes';
  end if;

  -- Opportunistic clean-up keeps the table small without a scheduled job.
  delete from public.member_pokes as pokes
  where pokes.created_at < now() - interval '7 days';

  insert into public.member_pokes (group_id, from_user, to_user)
  values (target_group, actor, target_user)
  returning member_pokes.created_at into created;

  return created;
end;
$$;

create or replace function public.fetch_my_pokes(
  target_group uuid,
  since timestamptz default now() - interval '10 minutes'
)
returns table (id bigint, from_user uuid, created_at timestamptz)
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
#variable_conflict use_column
declare
  actor uuid := auth.uid();
  cutoff timestamptz := coalesce(since, now() - interval '10 minutes');
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_group is null then raise exception 'group is required'; end if;

  if not exists (
    select 1
    from public.group_members as members
    where members.group_id = target_group
      and members.user_id = actor
  ) then
    raise exception 'not a member of this group';
  end if;

  -- Select and mark in one statement: the returned rows are exactly the rows
  -- marked seen, and SKIP LOCKED keeps two concurrent fetches of the same
  -- account from returning the same poke twice.
  return query
  with picked as (
    select pokes.id
    from public.member_pokes as pokes
    where pokes.to_user = actor
      and pokes.group_id = target_group
      and pokes.seen_at is null
      and pokes.created_at > cutoff
    order by pokes.created_at, pokes.id
    limit 50
    for update skip locked
  ),
  marked as (
    update public.member_pokes as pokes
    set seen_at = now()
    from picked
    where pokes.id = picked.id
    returning pokes.id as poke_id,
      pokes.from_user as poke_from,
      pokes.created_at as poke_created_at
  )
  select marked.poke_id, marked.poke_from, marked.poke_created_at
  from marked
  order by marked.poke_created_at, marked.poke_id;
end;
$$;

revoke all on function public.poke_room_member(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.poke_room_member(uuid, uuid)
  to authenticated;

revoke all on function public.fetch_my_pokes(uuid, timestamptz)
  from public, anon, authenticated;
grant execute on function public.fetch_my_pokes(uuid, timestamptz)
  to authenticated;

commit;
