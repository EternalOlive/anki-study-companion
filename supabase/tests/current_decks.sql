-- Run after schema.sql and 20261003_current_decks.sql on a TEST database.
-- All fixtures and changes are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  person uuid := gen_random_uuid();
  peer uuid := gen_random_uuid();
  stranger uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  device_a uuid := gen_random_uuid();
  device_b uuid := gen_random_uuid();
  peer_device uuid := gen_random_uuid();
  deck record;
begin
  insert into auth.users(id) values (person), (peer), (stranger);
  insert into public.study_groups(id, name, owner_id)
    values (room, 'current deck regression', person);
  insert into public.group_members(group_id, user_id)
    values (room, person), (room, peer);

  perform set_config('request.jwt.claim.sub', person::text, true);
  perform public.set_current_deck(room, device_a, 'Languages::Korean');
  select * into deck from public.get_group_current_decks(room);
  assert deck.user_id = person;
  assert deck.current_deck_name = 'Languages::Korean';
  assert deck.deck_updated_at is not null;

  update public.device_current_decks
    set updated_at = now() - interval '30 seconds'
    where group_id = room and user_id = person and device_id = device_a;
  perform public.set_current_deck(room, device_b, 'Medicine');
  select * into deck from public.get_group_current_decks(room)
    where user_id = person;
  assert deck.current_deck_name = 'Medicine';

  perform set_config('request.jwt.claim.sub', peer::text, true);
  perform public.set_current_deck(room, peer_device, 'Shared::Exam');
  perform set_config('request.jwt.claim.sub', person::text, true);
  assert (select count(*) from public.get_group_current_decks(room)) = 2;

  update public.device_current_decks
    set updated_at = now() - interval '91 seconds'
    where group_id = room and user_id = peer;
  assert not exists (
    select 1 from public.get_group_current_decks(room) where user_id = peer
  );

  perform public.set_current_deck(room, device_b, null);
  select * into deck from public.get_group_current_decks(room)
    where user_id = person;
  assert deck.current_deck_name = 'Languages::Korean';
  perform public.set_current_deck(room, device_a, null);
  assert not exists (
    select 1 from public.get_group_current_decks(room) where user_id = person
  );

  begin
    perform public.set_current_deck(room, device_a, repeat('x', 301));
    raise exception 'long deck name accepted';
  exception when raise_exception then
    if sqlerrm <> 'deck name too long' then raise; end if;
  end;
  begin
    perform public.set_current_deck(room, null, 'Deck');
    raise exception 'null device accepted';
  exception when raise_exception then
    if sqlerrm <> 'source device required' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', stranger::text, true);
  begin
    perform public.set_current_deck(room, device_a, 'Private');
    raise exception 'nonmember write accepted';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;
  begin
    perform public.get_group_current_decks(room);
    raise exception 'nonmember read accepted';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform public.get_group_current_decks(room);
    raise exception 'unauthenticated read accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;

  assert not has_table_privilege(
    'authenticated', 'public.device_current_decks', 'SELECT');
  assert not has_table_privilege(
    'authenticated', 'public.device_current_decks', 'INSERT');
  assert not has_function_privilege(
    'anon', 'public.set_current_deck(uuid,uuid,text)', 'EXECUTE');
  assert not has_function_privilege(
    'anon', 'public.get_group_current_decks(uuid)', 'EXECUTE');

  perform set_config('request.jwt.claim.sub', person::text, true);
  perform public.set_current_deck(room, device_a, 'Delete with membership');
  delete from public.group_members
    where group_id = room and user_id = person;
  assert not exists (
    select 1 from public.device_current_decks
    where group_id = room and user_id = person
  );
end;
$$;
rollback;
