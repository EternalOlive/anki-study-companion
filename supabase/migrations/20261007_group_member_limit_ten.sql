-- Migration: 20261007_group_member_limit_ten.sql
-- 1. Increase study group member limit from 8 to 10
-- 2. Update list_public_study_groups to allow rooms with up to 10 members
-- 3. Mark room 35FU as public so new users can discover and join it

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

-- 1. Ensure room 35FU is public
update public.study_groups
set is_public = true
where upper(invite_code) = '35FU';

-- 2. Update trigger to allow up to 10 members
create or replace function public.guard_study_group_membership()
returns trigger
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
begin
  perform pg_advisory_xact_lock(hashtextextended(new.group_id::text, 0));

  perform 1
  from public.study_groups as groups
  where groups.id = new.group_id
  for key share;

  if exists (
    select 1
    from public.study_group_bans as bans
    where bans.group_id = new.group_id
      and bans.user_id = new.user_id
  ) then
    raise exception 'blocked from this group';
  end if;

  if not exists (
    select 1
    from public.group_members as existing
    where existing.group_id = new.group_id
      and existing.user_id = new.user_id
  ) and (
    select count(*)
    from public.group_members as members
    where members.group_id = new.group_id
  ) >= 10 then
    raise exception 'study group is full';
  end if;

  return new;
end;
$$;

revoke all on function public.guard_study_group_membership()
  from public, anon, authenticated;

-- 3. Update list_public_study_groups to allow rooms with up to 10 members
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
    and coalesce(gmc.total_members, 0) < 10
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
