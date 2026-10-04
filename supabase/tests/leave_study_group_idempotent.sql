-- Run after 20261004_leave_study_group_idempotent.sql and the moderation
-- migration on a TEST database. All fixtures and mutations are rolled back.
begin;
do $$
declare
  owner_user uuid := gen_random_uuid();
  next_owner uuid := gen_random_uuid();
  member_user uuid := gen_random_uuid();
  blocked_user uuid := gen_random_uuid();
  outsider_user uuid := gen_random_uuid();
  lone_owner uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  lone_room uuid := gen_random_uuid();
  invite text;
  lone_invite text;
begin
  loop
    invite := public.generate_invite_code();
    exit when not exists (
      select 1 from public.study_groups where invite_code = invite
    );
  end loop;
  loop
    lone_invite := public.generate_invite_code();
    exit when lone_invite <> invite and not exists (
      select 1 from public.study_groups where invite_code = lone_invite
    );
  end loop;

  insert into auth.users(id) values
    (owner_user), (next_owner), (member_user), (blocked_user),
    (outsider_user), (lone_owner);
  insert into public.study_groups(id, name, invite_code, owner_id) values
    (room, 'idempotent leave regression', invite, owner_user),
    (lone_room, 'last owner regression', lone_invite, lone_owner);
  insert into public.group_members(group_id, user_id, joined_at) values
    (room, owner_user, '2026-01-01 00:00:00+00'),
    (room, next_owner, '2026-01-01 00:01:00+00'),
    (room, member_user, '2026-01-01 00:02:00+00'),
    (room, blocked_user, '2026-01-01 00:03:00+00'),
    (lone_room, lone_owner, '2026-01-01 00:00:00+00');
  insert into public.daily_stats(
    group_id, user_id, study_day, active_seconds, answer_count
  ) values
    (room, next_owner, current_date, 120, 2),
    (room, member_user, current_date, 60, 1);

  -- A normal member leaves; retrying is a successful no-op.
  perform set_config('request.jwt.claim.sub', member_user::text, true);
  perform public.leave_study_group(room);
  perform public.leave_study_group(room);
  assert not exists (
    select 1 from public.group_members
    where group_id = room and user_id = member_user
  );
  assert not exists (
    select 1 from public.daily_stats
    where group_id = room and user_id = member_user
  );
  assert (select owner_id from public.study_groups where id = room) = owner_user;

  -- A nonmember cannot remove or alter another participant's records.
  perform set_config('request.jwt.claim.sub', outsider_user::text, true);
  perform public.leave_study_group(room);
  assert exists (
    select 1 from public.group_members
    where group_id = room and user_id = next_owner
  );
  assert exists (
    select 1 from public.daily_stats
    where group_id = room and user_id = next_owner
      and active_seconds = 120 and answer_count = 2
  );
  assert (select owner_id from public.study_groups where id = room) = owner_user;

  -- Moderation removes membership. A later leave retry must not remove the ban
  -- or make re-entry possible.
  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  perform public.moderate_study_group_member(room, blocked_user, true);
  perform set_config('request.jwt.claim.sub', blocked_user::text, true);
  perform public.leave_study_group(room);
  assert exists (
    select 1 from public.study_group_bans
    where group_id = room and user_id = blocked_user
  );
  assert not exists (
    select 1 from public.group_members
    where group_id = room and user_id = blocked_user
  );
  begin
    perform public.join_study_group_safe(invite);
    raise exception 'blocked member rejoined after idempotent leave';
  exception when raise_exception then
    if sqlerrm <> 'blocked from this group' then raise; end if;
  end;

  -- Existing owner behavior is preserved: the oldest remaining member becomes
  -- owner, and a room with no successor is deleted.
  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  perform public.leave_study_group(room);
  assert (select owner_id from public.study_groups where id = room) = next_owner;
  assert exists (
    select 1 from public.group_members
    where group_id = room and user_id = next_owner
  );

  perform set_config('request.jwt.claim.sub', lone_owner::text, true);
  perform public.leave_study_group(lone_room);
  assert not exists (
    select 1 from public.study_groups where id = lone_room
  );
  perform public.leave_study_group(lone_room);

  -- Authentication and executable privileges remain explicit.
  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform public.leave_study_group(room);
    raise exception 'unauthenticated leave accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;
  assert not has_function_privilege(
    'anon', 'public.leave_study_group(uuid)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.leave_study_group(uuid)', 'EXECUTE');
  assert position(
    'SET search_path TO ''''' in pg_get_functiondef(
      'public.leave_study_group(uuid)'::regprocedure
    )
  ) > 0;
  assert position(
    'FOR UPDATE' in upper(pg_get_functiondef(
      'public.leave_study_group(uuid)'::regprocedure
    ))
  ) > 0;
end;
$$;
rollback;
