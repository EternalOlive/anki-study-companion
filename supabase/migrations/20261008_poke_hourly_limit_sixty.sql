-- Migration: Increase hourly poke limit from 30 to 60.

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

  -- Membership already excludes banned accounts. Check the ban table
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

  -- Up to 60 pokes per hour per sender.
  if (
    select count(*)
    from public.member_pokes as pokes
    where pokes.from_user = actor
      and pokes.created_at > now() - interval '1 hour'
  ) >= 60 then
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
