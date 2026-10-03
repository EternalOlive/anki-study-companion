-- Run after schema.sql or 20261004_invite_code_protection.sql on a TEST database.
-- All fixtures and mutations are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  owner_user uuid := gen_random_uuid();
  joining_user uuid := gen_random_uuid();
  limited_user uuid := gen_random_uuid();
  outsider uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  old_code text := 'A2BC';
  new_code text;
  result jsonb;
  attempt integer;
begin
  insert into auth.users(id) values
    (owner_user), (joining_user), (limited_user), (outsider);
  insert into public.study_groups(id, name, invite_code, owner_id)
    values (room, 'invite protection regression', old_code, owner_user);
  insert into public.group_members(group_id, user_id)
    values (room, owner_user);

  perform set_config('request.jwt.claim.sub', joining_user::text, true);
  result := public.join_study_group_safe('bad!');
  assert not (result->>'ok')::boolean;
  assert result->>'error' = 'INVALID_INVITE_CODE';
  assert (select attempt_count from public.invite_join_rate_limits
    where user_id = joining_user) = 1;

  result := public.join_study_group_safe(lower(old_code));
  assert (result->>'ok')::boolean;
  assert (result->>'group_id')::uuid = room;
  assert exists (select 1 from public.group_members
    where group_id = room and user_id = joining_user);

  assert (select attempt_count from public.invite_join_rate_limits
    where user_id = joining_user) = 2;

  perform set_config('request.jwt.claim.sub', limited_user::text, true);
  for attempt in 1..8 loop
    result := public.join_study_group_safe('ZZZZ');
    assert result->>'error' = 'INVALID_INVITE_CODE';
  end loop;
  result := public.join_study_group_safe(old_code);
  assert not (result->>'ok')::boolean;
  assert result->>'error' = 'TOO_MANY_ATTEMPTS';
  assert (result->>'retry_after')::integer > 0;
  assert not exists (select 1 from public.group_members
    where group_id = room and user_id = limited_user);
  assert (select attempt_count from public.invite_join_rate_limits
    where user_id = limited_user) = 9;

  update public.invite_join_rate_limits
  set window_started_at = now() - interval '11 minutes'
  where user_id = limited_user;
  result := public.join_study_group_safe(old_code);
  assert (result->>'ok')::boolean;
  assert (select attempt_count from public.invite_join_rate_limits
    where user_id = limited_user) = 1;

  perform set_config('request.jwt.claim.sub', outsider::text, true);
  begin
    perform public.rotate_study_group_invite(room);
    raise exception 'non-owner rotated invite code';
  exception when raise_exception then
    if sqlerrm <> 'only the group owner can rotate the invite code' then raise; end if;
  end;
  assert (select invite_code from public.study_groups where id = room) = old_code;

  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  new_code := public.rotate_study_group_invite(room);
  assert new_code ~ '^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}$';
  assert new_code <> old_code;
  assert (select invite_code from public.study_groups where id = room) = new_code;

  perform set_config('request.jwt.claim.sub', outsider::text, true);
  result := public.join_study_group_safe(old_code);
  assert result->>'error' = 'INVALID_INVITE_CODE';
  result := public.join_study_group_safe(new_code);
  assert (result->>'ok')::boolean;
  assert exists (select 1 from public.group_members
    where group_id = room and user_id = outsider);

  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform public.join_study_group_safe(old_code);
    raise exception 'unauthenticated join accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;
  begin
    perform public.rotate_study_group_invite(room);
    raise exception 'unauthenticated rotation accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;

  assert not has_table_privilege(
    'anon', 'public.invite_join_rate_limits', 'SELECT');
  assert not has_table_privilege(
    'authenticated', 'public.invite_join_rate_limits', 'SELECT');
  assert not has_table_privilege(
    'authenticated', 'public.invite_join_rate_limits', 'INSERT');
  assert not has_function_privilege(
    'anon', 'public.join_study_group_safe(text)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.join_study_group_safe(text)', 'EXECUTE');
  assert to_regprocedure('public.join_study_group(text)') is null
    or not has_function_privilege(
      'authenticated', to_regprocedure('public.join_study_group(text)'), 'EXECUTE');
  assert not has_function_privilege(
    'anon', 'public.rotate_study_group_invite(uuid)', 'EXECUTE');
end;
$$;
rollback;
