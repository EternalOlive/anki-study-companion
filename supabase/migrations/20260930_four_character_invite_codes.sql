create or replace function public.generate_invite_code()
returns text
language sql
volatile
set search_path = ''
as $$
  select string_agg(
    substr('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', 1 + floor(random() * 32)::integer, 1),
    ''
  )
  from generate_series(1, 4);
$$;

revoke all on function public.generate_invite_code() from public;

alter table public.study_groups
  alter column invite_code set default public.generate_invite_code();

create or replace function public.create_study_group(group_name text)
returns table (group_id uuid, invite_code text)
language plpgsql
security definer
set search_path = public
as $$
declare
  created_group public.study_groups;
  attempt integer;
begin
  if auth.uid() is null then raise exception 'authentication required'; end if;
  for attempt in 1..10 loop
    begin
      insert into public.study_groups(name, invite_code, owner_id)
      values (trim(group_name), public.generate_invite_code(), auth.uid())
      returning * into created_group;
      exit;
    exception when unique_violation then
      if attempt = 10 then
        raise exception 'could not allocate invite code';
      end if;
    end;
  end loop;
  insert into public.group_members(group_id, user_id)
  values (created_group.id, auth.uid());
  return query select created_group.id, created_group.invite_code;
end;
$$;

revoke all on function public.create_study_group(text) from public;
grant execute on function public.create_study_group(text) to authenticated;
