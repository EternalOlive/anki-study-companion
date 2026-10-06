-- Run after 20261006_member_pokes.sql on a TEST database.
-- All fixtures and mutations are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  owner_user uuid := gen_random_uuid();
  friend_user uuid := gen_random_uuid();
  outsider_user uuid := gen_random_uuid();
  banned_user uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  other_room uuid := gen_random_uuid();
  invite text;
  other_invite text;
  poked_at timestamptz;
  fetched_count integer;
  fetched_from uuid;
  i integer;
begin
  loop
    invite := public.generate_invite_code();
    exit when not exists (
      select 1 from public.study_groups where invite_code = invite
    );
  end loop;
  loop
    other_invite := public.generate_invite_code();
    exit when other_invite <> invite and not exists (
      select 1 from public.study_groups where invite_code = other_invite
    );
  end loop;

  insert into auth.users(id) values
    (owner_user), (friend_user), (outsider_user), (banned_user);
  insert into public.study_groups(id, name, invite_code, owner_id) values
    (room, 'poke regression', invite, owner_user),
    (other_room, 'poke regression 2', other_invite, outsider_user);
  insert into public.group_members(group_id, user_id) values
    (room, owner_user), (room, friend_user), (room, banned_user),
    (other_room, outsider_user);

  -- Unauthenticated calls are rejected.
  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform public.poke_room_member(room, friend_user);
    raise exception 'unauthenticated poke accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;
  begin
    perform * from public.fetch_my_pokes(room);
    raise exception 'unauthenticated fetch accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;

  -- A member pokes another member; the receiver sees it exactly once.
  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  poked_at := public.poke_room_member(room, friend_user);
  assert poked_at is not null;
  assert exists (
    select 1 from public.member_pokes
    where group_id = room and from_user = owner_user and to_user = friend_user
      and seen_at is null
  );

  -- Same pair within 60 seconds is refused.
  begin
    perform public.poke_room_member(room, friend_user);
    raise exception 'second poke within 60 seconds accepted';
  exception when raise_exception then
    if sqlerrm <> 'poke too soon' then raise; end if;
  end;

  -- Self-poke, outsider target and non-member caller are refused.
  begin
    perform public.poke_room_member(room, owner_user);
    raise exception 'self poke accepted';
  exception when raise_exception then
    if sqlerrm <> 'cannot poke yourself' then raise; end if;
  end;
  begin
    perform public.poke_room_member(room, outsider_user);
    raise exception 'poke to a non-member accepted';
  exception when raise_exception then
    if sqlerrm <> 'target is not in this group' then raise; end if;
  end;
  perform set_config('request.jwt.claim.sub', outsider_user::text, true);
  begin
    perform public.poke_room_member(room, friend_user);
    raise exception 'non-member poked into a room';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;
  begin
    perform * from public.fetch_my_pokes(room);
    raise exception 'non-member fetched room pokes';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;

  -- The sender cannot read the receiver's pokes; the receiver can, once.
  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  assert (select count(*) from public.fetch_my_pokes(room)) = 0;

  perform set_config('request.jwt.claim.sub', friend_user::text, true);
  select count(*), min(from_user::text)::uuid into fetched_count, fetched_from
  from public.fetch_my_pokes(room);
  assert fetched_count = 1;
  assert fetched_from = owner_user;
  assert not exists (
    select 1 from public.member_pokes
    where group_id = room and to_user = friend_user and seen_at is null
  );
  assert (select count(*) from public.fetch_my_pokes(room)) = 0;

  -- Pokes older than `since` are not returned (and stay unseen).
  insert into public.member_pokes(group_id, from_user, to_user, created_at)
  values (room, owner_user, friend_user, now() - interval '30 minutes');
  assert (select count(*) from public.fetch_my_pokes(room)) = 0;
  assert (
    select count(*) from public.fetch_my_pokes(room, now() - interval '1 hour')
  ) = 1;

  -- A banned account is removed from the room and cannot poke or be poked.
  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  perform public.moderate_study_group_member(room, banned_user, true);
  begin
    perform public.poke_room_member(room, banned_user);
    raise exception 'poke to a banned account accepted';
  exception when raise_exception then
    if sqlerrm <> 'target is not in this group' then raise; end if;
  end;
  perform set_config('request.jwt.claim.sub', banned_user::text, true);
  begin
    perform public.poke_room_member(room, friend_user);
    raise exception 'banned account poked';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;

  -- Hourly cap: 30 pokes per sender per hour across all rooms.
  perform set_config('request.jwt.claim.sub', friend_user::text, true);
  for i in 1..30 loop
    insert into public.member_pokes(group_id, from_user, to_user, created_at)
    values (room, friend_user, owner_user, now() - interval '50 minutes');
  end loop;
  begin
    perform public.poke_room_member(room, owner_user);
    raise exception 'hourly poke cap not enforced';
  exception when raise_exception then
    if sqlerrm <> 'too many pokes' then raise; end if;
  end;

  -- Opportunistic 7-day clean-up runs on the next accepted poke.
  delete from public.member_pokes where from_user = friend_user;
  insert into public.member_pokes(group_id, from_user, to_user, created_at)
  values (room, owner_user, friend_user, now() - interval '8 days');
  poked_at := public.poke_room_member(room, owner_user);
  assert poked_at is not null;
  assert not exists (
    select 1 from public.member_pokes
    where created_at < now() - interval '7 days'
  );

  -- Clients never get direct table access.
  assert not has_table_privilege('anon', 'public.member_pokes', 'SELECT');
  assert not has_table_privilege('authenticated', 'public.member_pokes', 'SELECT');
  assert not has_table_privilege('authenticated', 'public.member_pokes', 'INSERT');
  assert not has_table_privilege('authenticated', 'public.member_pokes', 'UPDATE');
  assert not has_table_privilege('authenticated', 'public.member_pokes', 'DELETE');
  assert (
    select relrowsecurity from pg_class
    where oid = 'public.member_pokes'::regclass
  );
  assert not has_function_privilege(
    'anon', 'public.poke_room_member(uuid,uuid)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.poke_room_member(uuid,uuid)', 'EXECUTE');
  assert not has_function_privilege(
    'anon', 'public.fetch_my_pokes(uuid,timestamptz)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.fetch_my_pokes(uuid,timestamptz)', 'EXECUTE');
end;
$$;
rollback;
