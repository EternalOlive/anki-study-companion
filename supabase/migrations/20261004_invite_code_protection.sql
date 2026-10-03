-- Keep short invite codes convenient while slowing account-based guessing.
-- Invalid attempts return an envelope instead of raising so the counter commits.
begin;

create table if not exists public.invite_join_rate_limits (
  user_id uuid primary key references auth.users(id) on delete cascade,
  window_started_at timestamptz not null,
  attempt_count integer not null check (attempt_count >= 1)
);

alter table public.invite_join_rate_limits enable row level security;
revoke all on public.invite_join_rate_limits from public, anon, authenticated;

create or replace function public.join_study_group_safe(code text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  target_group uuid;
  rate_record public.invite_join_rate_limits%rowtype;
  retry_after integer;
begin
  if actor is null then raise exception 'authentication required'; end if;

  insert into public.invite_join_rate_limits as limits (
    user_id, window_started_at, attempt_count
  ) values (
    actor, now(), 1
  ) on conflict (user_id) do update set
    window_started_at = case
      when limits.window_started_at <= now() - interval '10 minutes'
        then now()
      else limits.window_started_at
    end,
    attempt_count = case
      when limits.window_started_at <= now() - interval '10 minutes'
        then 1
      else limits.attempt_count + 1
    end
  returning * into rate_record;

  if rate_record.attempt_count > 8 then
    retry_after := greatest(1, ceil(extract(epoch from (
      rate_record.window_started_at + interval '10 minutes' - now()
    )))::integer);
    return jsonb_build_object(
      'ok', false,
      'error', 'TOO_MANY_ATTEMPTS',
      'retry_after', retry_after
    );
  end if;

  if code is null or upper(trim(code)) !~ '^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}$' then
    return jsonb_build_object(
      'ok', false,
      'error', 'INVALID_INVITE_CODE',
      'retry_after', 0
    );
  end if;

  select sg.id into target_group
  from public.study_groups as sg
  where sg.invite_code = upper(trim(code))
  for key share;

  if target_group is null then
    return jsonb_build_object(
      'ok', false,
      'error', 'INVALID_INVITE_CODE',
      'retry_after', 0
    );
  end if;

  insert into public.group_members(group_id, user_id)
  values (target_group, actor)
  on conflict do nothing;

  return jsonb_build_object(
    'ok', true,
    'group_id', target_group
  );
end;
$$;

create or replace function public.rotate_study_group_invite(target_group uuid)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  owner uuid;
  current_code text;
  new_code text;
  attempt integer;
begin
  if actor is null then raise exception 'authentication required'; end if;

  select sg.owner_id, sg.invite_code into owner, current_code
  from public.study_groups as sg
  where sg.id = target_group
  for update;

  if owner is null or owner <> actor then
    raise exception 'only the group owner can rotate the invite code';
  end if;

  for attempt in 1..10 loop
    begin
      new_code := public.generate_invite_code();
      if new_code = current_code then continue; end if;
      update public.study_groups
      set invite_code = new_code
      where id = target_group;
      return new_code;
    exception when unique_violation then
      if attempt = 10 then
        raise exception 'could not allocate invite code';
      end if;
    end;
  end loop;

  raise exception 'could not allocate invite code';
end;
$$;

revoke all on function public.join_study_group_safe(text)
  from public, anon, authenticated;
grant execute on function public.join_study_group_safe(text) to authenticated;
revoke all on function public.join_study_group(text)
  from public, anon, authenticated;
revoke all on function public.rotate_study_group_invite(uuid)
  from public, anon, authenticated;
grant execute on function public.rotate_study_group_invite(uuid) to authenticated;

commit;
