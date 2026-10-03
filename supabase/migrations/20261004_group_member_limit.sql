-- Keep private study rooms small and bound fan-out costs.
-- The owner counts toward the ten-person limit.
begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

create or replace function public.guard_study_group_membership()
returns trigger
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
begin
  -- Serialize membership counts without conflicting with the join RPC's
  -- existing FOR KEY SHARE room lock. Hash collisions only serialize extra
  -- rooms; they cannot let a room exceed its limit.
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

commit;
