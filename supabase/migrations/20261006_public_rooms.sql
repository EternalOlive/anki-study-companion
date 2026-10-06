-- Migration: 20261006_public_rooms.sql
-- 1. Add is_public column to study_groups (defaults to false)
-- 2. Update create_study_group to accept is_public
-- 3. Add list_public_study_groups RPC:
--    - filters for is_public = true and member count < 8
--    - counts current studying members
--    - sorts by: matches user timezone first, then active studying count desc, then latest activity

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

alter table public.study_groups
  add column if not exists is_public boolean not null default false;

create index if not exists idx_study_groups_public
  on public.study_groups(is_public)
  where is_public = true;

-- Update create_study_group
create or replace function public.create_study_group(
  group_name text,
  room_timezone text default 'Asia/Seoul',
  room_is_public boolean default false
)
returns table (
  group_id uuid,
  invite_code text,
  time_zone text,
  day_start_hour smallint,
  is_public boolean
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  normalized_timezone text := btrim(room_timezone);
  created_group public.study_groups;
  attempt integer;
begin
  if actor is null then raise exception 'authentication required'; end if;
  if group_name is null or char_length(btrim(group_name)) not between 1 and 50 then
    raise exception 'invalid group name';
  end if;
  if normalized_timezone is null or normalized_timezone = '' or not exists (
    select 1 from pg_catalog.pg_timezone_names zones
    where zones.name = normalized_timezone
  ) then
    raise exception 'invalid room time zone';
  end if;

  for attempt in 1..10 loop
    begin
      insert into public.study_groups(
        name, invite_code, owner_id, time_zone, day_start_hour, is_public
      ) values (
        btrim(group_name), public.generate_invite_code(), actor,
        normalized_timezone, 4, coalesce(room_is_public, false)
      ) returning * into created_group;
      exit;
    exception when unique_violation then
      if attempt = 10 then raise exception 'could not allocate invite code'; end if;
    end;
  end loop;

  insert into public.group_members(group_id, user_id)
  values (created_group.id, actor);

  return query select created_group.id, created_group.invite_code,
    created_group.time_zone, created_group.day_start_hour, created_group.is_public;
end;
$$;

revoke all on function public.create_study_group(text, text, boolean)
  from public, anon, authenticated;
grant execute on function public.create_study_group(text, text, boolean)
  to authenticated;

-- Function: list_public_study_groups
create or replace function public.list_public_study_groups(
  user_timezone text default 'Asia/Seoul'
)
returns table (
  id uuid,
  name text,
  invite_code text,
  time_zone text,
  member_count integer,
  studying_count integer,
  owner_id uuid,
  created_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  normalized_tz text := btrim(coalesce(user_timezone, ''));
begin
  if actor is null then raise exception 'authentication required'; end if;

  return query
  with group_member_counts as (
    select gm.group_id, count(*)::integer as total_members
    from public.group_members gm
    group by gm.group_id
  ),
  current_studying as (
    select dds.group_id, count(distinct dds.user_id)::integer as active_studying
    from public.device_daily_stats dds
    where dds.status = 'studying'
      and dds.updated_at >= now() - interval '90 seconds'
    group by dds.group_id
  )
  select
    sg.id,
    sg.name,
    sg.invite_code,
    sg.time_zone,
    coalesce(gmc.total_members, 0) as member_count,
    coalesce(cs.active_studying, 0) as studying_count,
    sg.owner_id,
    sg.created_at
  from public.study_groups sg
  left join group_member_counts gmc on gmc.group_id = sg.id
  left join current_studying cs on cs.group_id = sg.id
  where sg.is_public = true
    and coalesce(gmc.total_members, 0) < 8
    and not exists (
      select 1 from public.study_group_bans sgb
      where sgb.group_id = sg.id and sgb.user_id = actor
    )
  order by
    case when sg.time_zone = normalized_tz then 0 else 1 end,
    coalesce(cs.active_studying, 0) desc,
    coalesce(gmc.total_members, 0) desc,
    sg.created_at desc
  limit 30;
end;
$$;

revoke all on function public.list_public_study_groups(text)
  from public, anon, authenticated;
grant execute on function public.list_public_study_groups(text)
  to authenticated;

commit;
