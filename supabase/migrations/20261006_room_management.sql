-- Migration: 20261006_room_management.sql
-- Room management functions:
-- 1. update_room_timezone (owner-only room timezone update)
-- 2. transfer_room_ownership (owner-only ownership transfer)
-- 3. kick_room_member (owner-only kick member, excluding self)
-- 4. cleanup_inactive_members (owner-only cleanup members inactive for 14+ days)

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

-- Update guard_study_group_calendar to allow room time zone updates
-- while keeping the 04:00 day boundary strictly immutable.
create or replace function public.guard_study_group_calendar()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.time_zone := btrim(new.time_zone);
  if new.time_zone is null or new.time_zone = '' or not exists (
    select 1 from pg_catalog.pg_timezone_names zones
    where zones.name = new.time_zone
  ) then
    raise exception 'invalid room time zone';
  end if;
  if new.day_start_hour <> 4 then
    raise exception 'invalid room day boundary';
  end if;
  if tg_op = 'UPDATE' and (
    new.day_start_hour is distinct from old.day_start_hour
  ) then
    raise exception 'room day boundary is immutable';
  end if;
  return new;
end;
$$;

revoke all on function public.guard_study_group_calendar()
  from public, anon, authenticated;

-- 1. update_room_timezone
create or replace function public.update_room_timezone(
  target_group uuid,
  new_timezone text
)
returns void
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  actor uuid := auth.uid();
  owner uuid;
  normalized_zone text := btrim(new_timezone);
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_group is null or normalized_zone is null or normalized_zone = '' then
    raise exception 'group and valid timezone are required';
  end if;

  select groups.owner_id into owner
  from public.study_groups as groups
  where groups.id = target_group
  for update;

  if owner is null or owner <> actor then
    raise exception 'only room owner can update timezone';
  end if;

  if not exists (
    select 1 from pg_catalog.pg_timezone_names zones
    where zones.name = normalized_zone
  ) then
    raise exception 'invalid room time zone';
  end if;

  update public.study_groups
  set time_zone = normalized_zone
  where id = target_group;
end;
$$;

-- 2. transfer_room_ownership
create or replace function public.transfer_room_ownership(
  target_group uuid,
  new_owner_id uuid
)
returns void
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  actor uuid := auth.uid();
  owner uuid;
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_group is null or new_owner_id is null then
    raise exception 'group and new owner are required';
  end if;

  select groups.owner_id into owner
  from public.study_groups as groups
  where groups.id = target_group
  for update;

  if owner is null or owner <> actor then
    raise exception 'only room owner can transfer ownership';
  end if;

  if new_owner_id = actor then
    return;
  end if;

  if not exists (
    select 1
    from public.group_members as members
    where members.group_id = target_group
      and members.user_id = new_owner_id
  ) then
    raise exception 'new owner must be a member of the group';
  end if;

  update public.study_groups
  set owner_id = new_owner_id
  where id = target_group;
end;
$$;

-- 3. kick_room_member
create or replace function public.kick_room_member(
  target_group uuid,
  target_user uuid
)
returns void
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  actor uuid := auth.uid();
  owner uuid;
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_group is null or target_user is null then
    raise exception 'group and target user are required';
  end if;

  select groups.owner_id into owner
  from public.study_groups as groups
  where groups.id = target_group
  for update;

  if owner is null or owner <> actor then
    raise exception 'only room owner can kick members';
  end if;

  if target_user = actor then
    raise exception 'the group owner cannot kick themselves';
  end if;

  delete from public.daily_stats
  where group_id = target_group and user_id = target_user;

  delete from public.group_members
  where group_id = target_group and user_id = target_user;
end;
$$;

-- 4. cleanup_inactive_members
create or replace function public.cleanup_inactive_members(
  target_group uuid,
  days_inactive integer default 14
)
returns integer
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  actor uuid := auth.uid();
  owner uuid;
  effective_days integer := coalesce(days_inactive, 14);
  cutoff timestamptz;
  removed_count integer := 0;
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_group is null then
    raise exception 'group is required';
  end if;
  if effective_days < 1 then
    effective_days := 14;
  end if;

  select groups.owner_id into owner
  from public.study_groups as groups
  where groups.id = target_group
  for update;

  if owner is null or owner <> actor then
    raise exception 'only room owner can cleanup inactive members';
  end if;

  cutoff := now() - (effective_days || ' days')::interval;

  with inactive as (
    select gm.user_id
    from public.group_members gm
    where gm.group_id = target_group
      and gm.user_id <> owner
      and coalesce(gm.joined_at, now()) <= cutoff
      and not exists (
        select 1
        from public.anki_review_day_markers m
        where m.group_id = target_group
          and m.user_id = gm.user_id
          and m.updated_at > cutoff
      )
      and not exists (
        select 1
        from public.device_daily_stats d
        where d.group_id = target_group
          and d.user_id = gm.user_id
          and d.updated_at > cutoff
      )
  ),
  deleted_stats as (
    delete from public.daily_stats ds
    where ds.group_id = target_group
      and ds.user_id in (select user_id from inactive)
  ),
  deleted_members as (
    delete from public.group_members gm
    where gm.group_id = target_group
      and gm.user_id in (select user_id from inactive)
    returning gm.user_id
  )
  select count(*)::integer into removed_count from deleted_members;

  return removed_count;
end;
$$;

-- Permissions and grants
revoke all on function public.update_room_timezone(uuid, text)
  from public, anon, authenticated;
grant execute on function public.update_room_timezone(uuid, text)
  to authenticated;

revoke all on function public.transfer_room_ownership(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.transfer_room_ownership(uuid, uuid)
  to authenticated;

revoke all on function public.kick_room_member(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.kick_room_member(uuid, uuid)
  to authenticated;

revoke all on function public.cleanup_inactive_members(uuid, integer)
  from public, anon, authenticated;
grant execute on function public.cleanup_inactive_members(uuid, integer)
  to authenticated;

commit;
