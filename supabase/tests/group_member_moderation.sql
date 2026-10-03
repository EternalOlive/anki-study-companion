-- Run after 20261004_group_member_moderation.sql on a TEST database.
-- All fixtures and mutations are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  owner_user uuid := gen_random_uuid();
  member_user uuid := gen_random_uuid();
  outsider_user uuid := gen_random_uuid();
  leaving_user uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  invite text;
  result jsonb;
begin
  loop
    invite := public.generate_invite_code();
    exit when not exists (
      select 1 from public.study_groups where invite_code = invite
    );
  end loop;

  insert into auth.users(id) values
    (owner_user), (member_user), (outsider_user), (leaving_user);
  insert into public.study_groups(id, name, invite_code, owner_id)
  values (room, 'moderation regression', invite, owner_user);
  insert into public.group_members(group_id, user_id) values
    (room, owner_user), (room, member_user), (room, leaving_user);
  insert into public.daily_stats(
    group_id, user_id, study_day, active_seconds, answer_count
  ) values (room, member_user, current_date, 60, 1);

  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  perform public.moderate_study_group_member(room, member_user, true);

  assert exists (
    select 1 from public.study_group_bans
    where group_id = room and user_id = member_user
      and blocked_by = owner_user
  );
  assert not exists (
    select 1 from public.group_members
    where group_id = room and user_id = member_user
  );
  assert not exists (
    select 1 from public.daily_stats
    where group_id = room and user_id = member_user
  );
  assert (select count(*) from public.list_study_group_bans(room)) = 1;
  assert (select user_id from public.list_study_group_bans(room)) = member_user;

  perform set_config('request.jwt.claim.sub', member_user::text, true);
  begin
    result := public.join_study_group_safe(invite);
    raise exception 'blocked member rejoined through the join RPC';
  exception when raise_exception then
    if sqlerrm <> 'blocked from this group' then raise; end if;
  end;
  assert not exists (
    select 1 from public.group_members
    where group_id = room and user_id = member_user
  );

  begin
    insert into public.group_members(group_id, user_id)
    values (room, member_user);
    raise exception 'blocked member bypassed the insert trigger';
  exception when raise_exception then
    if sqlerrm <> 'blocked from this group' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', outsider_user::text, true);
  begin
    perform public.moderate_study_group_member(room, leaving_user, true);
    raise exception 'non-owner moderated a member';
  exception when raise_exception then
    if sqlerrm <> 'only the group owner can moderate members' then raise; end if;
  end;
  begin
    perform * from public.list_study_group_bans(room);
    raise exception 'non-owner listed blocked members';
  exception when raise_exception then
    if sqlerrm <> 'only the group owner can list blocked members' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  begin
    perform public.moderate_study_group_member(room, owner_user, true);
    raise exception 'owner blocked themselves';
  exception when raise_exception then
    if sqlerrm <> 'the group owner cannot block themselves' then raise; end if;
  end;

  perform public.moderate_study_group_member(room, member_user, false);
  assert not exists (
    select 1 from public.study_group_bans
    where group_id = room and user_id = member_user
  );

  perform set_config('request.jwt.claim.sub', member_user::text, true);
  result := public.join_study_group_safe(invite);
  assert (result->>'ok')::boolean;
  assert exists (
    select 1 from public.group_members
    where group_id = room and user_id = member_user
  );

  -- Ordinary leave remains distinct from moderation and creates no ban.
  perform set_config('request.jwt.claim.sub', leaving_user::text, true);
  perform public.leave_study_group(room);
  assert not exists (
    select 1 from public.group_members
    where group_id = room and user_id = leaving_user
  );
  assert not exists (
    select 1 from public.study_group_bans
    where group_id = room and user_id = leaving_user
  );
  assert (select owner_id from public.study_groups where id = room) = owner_user;

  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform public.moderate_study_group_member(room, member_user, true);
    raise exception 'unauthenticated moderation accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;
  begin
    perform * from public.list_study_group_bans(room);
    raise exception 'unauthenticated ban listing accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;

  assert not has_table_privilege(
    'anon', 'public.study_group_bans', 'SELECT');
  assert not has_table_privilege(
    'authenticated', 'public.study_group_bans', 'SELECT');
  assert not has_table_privilege(
    'authenticated', 'public.study_group_bans', 'INSERT');
  assert not has_function_privilege(
    'anon',
    'public.moderate_study_group_member(uuid,uuid,boolean)',
    'EXECUTE');
  assert has_function_privilege(
    'authenticated',
    'public.moderate_study_group_member(uuid,uuid,boolean)',
    'EXECUTE');
  assert not has_function_privilege(
    'anon', 'public.list_study_group_bans(uuid)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.list_study_group_bans(uuid)', 'EXECUTE');
  assert exists (
    select 1 from pg_trigger
    where tgrelid = 'public.group_members'::regclass
      and tgname = 'guard_study_group_membership_insert'
      and not tgisinternal
  );
  assert position(
    'FOR KEY SHARE' in upper(pg_get_functiondef(
      'public.guard_study_group_membership()'::regprocedure
    ))
  ) > 0;
  assert position(
    'FOR UPDATE' in upper(pg_get_functiondef(
      'public.moderate_study_group_member(uuid,uuid,boolean)'::regprocedure
    ))
  ) > 0;
end;
$$;
rollback;
