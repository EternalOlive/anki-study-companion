create or replace function public.leave_study_group(target_group uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  current_user_id uuid := auth.uid();
  next_owner_id uuid;
begin
  if current_user_id is null then raise exception 'authentication required'; end if;
  if not exists (
    select 1 from public.group_members
    where group_id = target_group and user_id = current_user_id
  ) then
    raise exception 'not a member of this group';
  end if;

  delete from public.daily_stats
  where group_id = target_group and user_id = current_user_id;
  delete from public.group_members
  where group_id = target_group and user_id = current_user_id;

  if exists (
    select 1 from public.study_groups
    where id = target_group and owner_id = current_user_id
  ) then
    select user_id into next_owner_id
    from public.group_members
    where group_id = target_group
    order by joined_at asc, user_id asc
    limit 1;

    if next_owner_id is null then
      delete from public.study_groups where id = target_group;
    else
      update public.study_groups
      set owner_id = next_owner_id
      where id = target_group;
    end if;
  end if;
end;
$$;

revoke all on function public.leave_study_group(uuid) from public;
grant execute on function public.leave_study_group(uuid) to authenticated;
