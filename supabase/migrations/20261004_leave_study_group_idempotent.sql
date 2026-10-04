-- Make leaving safe to retry after moderation or a lost response.
-- The function remains actor-scoped: it never accepts a target user id.
begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

create or replace function public.leave_study_group(target_group uuid)
returns void
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  actor uuid := auth.uid();
  owner uuid;
  next_owner_id uuid;
begin
  if actor is null then raise exception 'authentication required'; end if;

  -- Serialize leave, ownership transfer, joins, and moderation on the group.
  -- A missing group or membership is already the requested end state.
  select groups.owner_id into owner
  from public.study_groups as groups
  where groups.id = target_group
  for update;

  if owner is null or not exists (
    select 1
    from public.group_members as members
    where members.group_id = target_group
      and members.user_id = actor
  ) then
    return;
  end if;

  delete from public.daily_stats as stats
  where stats.group_id = target_group
    and stats.user_id = actor;

  delete from public.group_members as members
  where members.group_id = target_group
    and members.user_id = actor;

  if owner = actor then
    select members.user_id into next_owner_id
    from public.group_members as members
    where members.group_id = target_group
    order by members.joined_at asc, members.user_id asc
    limit 1;

    if next_owner_id is null then
      delete from public.study_groups as groups
      where groups.id = target_group;
    else
      update public.study_groups as groups
      set owner_id = next_owner_id
      where groups.id = target_group;
    end if;
  end if;
end;
$$;

revoke all on function public.leave_study_group(uuid)
  from public, anon, authenticated;
grant execute on function public.leave_study_group(uuid)
  to authenticated;

commit;
