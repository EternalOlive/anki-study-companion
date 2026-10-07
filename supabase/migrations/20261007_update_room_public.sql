-- Migration: 20261007_update_room_public.sql
-- 1. Set room 35FU to public so new users can discover and join it.
-- 2. Add update_room_public RPC so room owners can toggle public/private status.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

-- 1. Ensure room 35FU is public
update public.study_groups
set is_public = true
where upper(invite_code) = '35FU';

-- 2. RPC for room owners to change public/private status
create or replace function public.update_room_public(
  target_group uuid,
  new_is_public boolean
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
  if target_group is null or new_is_public is null then
    raise exception 'group and is_public are required';
  end if;

  select groups.owner_id into owner
  from public.study_groups as groups
  where groups.id = target_group
  for update;

  if owner is null or owner <> actor then
    raise exception 'only room owner can update public status';
  end if;

  update public.study_groups
  set is_public = new_is_public
  where id = target_group;
end;
$$;

revoke all on function public.update_room_public(uuid, boolean)
  from public, anon, authenticated;
grant execute on function public.update_room_public(uuid, boolean)
  to authenticated;

commit;
