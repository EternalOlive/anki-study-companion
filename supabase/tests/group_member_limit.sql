-- Run after 20261004_group_member_limit_eight.sql on a TEST database.
-- All fixtures and mutations are rolled back.
begin;

do $$
declare
  owner_user uuid := gen_random_uuid();
  people uuid[] := array[
    gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
    gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
    gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
    gen_random_uuid(), gen_random_uuid()
  ];
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

  insert into auth.users(id)
  select user_id from unnest(array[owner_user] || people) as user_id;
  insert into public.study_groups(id, name, invite_code, owner_id)
  values (room, 'member limit regression', invite, owner_user);
  insert into public.group_members(group_id, user_id)
  values (room, owner_user);
  insert into public.group_members(group_id, user_id)
  select room, user_id from unnest(people[1:7]) as user_id;

  assert (select count(*) from public.group_members where group_id = room) = 8;

  perform set_config('request.jwt.claim.sub', people[8]::text, true);
  begin
    result := public.join_study_group_safe(invite);
    raise exception 'ninth member joined a full room';
  exception when raise_exception then
    if sqlerrm <> 'study group is full' then raise; end if;
  end;
  assert not exists (
    select 1 from public.group_members
    where group_id = room and user_id = people[8]
  );

  begin
    insert into public.group_members(group_id, user_id)
    values (room, people[9]);
    raise exception 'direct insert bypassed the room limit';
  exception when raise_exception then
    if sqlerrm <> 'study group is full' then raise; end if;
  end;

  -- An existing member's idempotent join remains valid at the limit.
  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  result := public.join_study_group_safe(invite);
  assert (result->>'ok')::boolean;
  assert (select count(*) from public.group_members where group_id = room) = 8;

  -- Leaving releases one slot for another account.
  perform set_config('request.jwt.claim.sub', people[1]::text, true);
  perform public.leave_study_group(room);
  perform set_config('request.jwt.claim.sub', people[8]::text, true);
  result := public.join_study_group_safe(invite);
  assert (result->>'ok')::boolean;
  assert (select count(*) from public.group_members where group_id = room) = 8;

  assert position(
    'PG_ADVISORY_XACT_LOCK' in upper(pg_get_functiondef(
      'public.guard_study_group_membership()'::regprocedure
    ))
  ) > 0;
  assert position(
    '>= 8' in pg_get_functiondef(
      'public.guard_study_group_membership()'::regprocedure
    )
  ) > 0;
end;
$$;

rollback;
