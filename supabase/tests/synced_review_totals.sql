-- Run after schema.sql, activate_device_sync and synced_review_totals.
-- All fixtures and changes are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  person uuid := gen_random_uuid();
  stranger uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  device uuid := gen_random_uuid();
  day date := (now() at time zone 'Asia/Seoul')::date;
  base_ms bigint;
  review_a text;
  review_b text;
  review_c text;
  aggregate record;
  receipt record;
  heartbeat timestamptz;
begin
  base_ms := (extract(epoch from (day::timestamp at time zone 'Asia/Seoul')) * 1000)::bigint;
  review_a := (base_ms + 1000)::text;
  review_b := (base_ms + 2000)::text;
  review_c := (base_ms + 3000)::text;

  insert into auth.users(id) values (person), (stranger);
  insert into public.study_groups(id, name, owner_id)
    values (room, 'native review regression', person);
  insert into public.group_members(group_id, user_id) values (room, person);
  perform set_config('request.jwt.claim.sub', person::text, true);

  -- Legacy totals remain visible until the first native day marker is stored.
  perform public.record_device_day(
    room, device, day, 1, 120, 8, 'studying', 60, 100);
  select * into aggregate from public.get_group_device_stats(room, day);
  assert aggregate.active_seconds = 120 and aggregate.answer_count = 8;
  assert aggregate.status = 'studying' and aggregate.time_goal_minutes = 60;

  -- Duplicate events from multiple PCs are counted once by collection/review/card.
  select * into receipt from public.sync_review_day(
    room, '1700000000000', day,
    jsonb_build_array(
      jsonb_build_object('id', review_a, 'card_id', '101', 'time_ms', 2500),
      jsonb_build_object('id', review_b, 'card_id', '102', 'time_ms', 1500)
    ), '[]'::jsonb);
  assert receipt.active_review_count = 2 and receipt.active_time_ms = 4000;
  select * into receipt from public.sync_review_day(
    room, '1700000000000', day,
    jsonb_build_array(
      jsonb_build_object('id', review_a, 'card_id', '101', 'time_ms', 999999),
      jsonb_build_object('id', review_b, 'card_id', '102', 'time_ms', 1500)
    ), '[]'::jsonb);
  assert receipt.active_review_count = 2 and receipt.active_time_ms = 4000;

  -- A second synced collection contributes, while legacy counters no longer do.
  perform public.sync_review_day(
    room, '1800000000000', day,
    jsonb_build_array(
      jsonb_build_object('id', review_c, 'card_id', '103', 'time_ms', 1000)
    ), '[]'::jsonb);
  select * into aggregate from public.get_group_device_stats(room, day);
  assert aggregate.active_seconds = 5 and aggregate.answer_count = 3;
  assert aggregate.status = 'studying' and aggregate.time_goal_minutes = 60;
  update public.device_daily_stats
    set updated_at = now() + interval '1 second'
    where group_id = room and user_id = person and device_id = device
    returning updated_at into heartbeat;
  select * into aggregate from public.get_group_device_stats(room, day);
  assert aggregate.updated_at = heartbeat;

  -- Explicit Undo wins over a stale snapshot, and a newer Redo can restore it.
  perform public.sync_review_day(
    room, '1700000000000', day, '[]'::jsonb,
    jsonb_build_array(jsonb_build_object(
      'id', review_a, 'card_id', '101', 'changed_at', 100
    )));
  perform public.sync_review_day(
    room, '1700000000000', day,
    jsonb_build_array(
      jsonb_build_object('id', review_a, 'card_id', '101', 'time_ms', 2500)
    ), '[]'::jsonb);
  select * into aggregate from public.get_group_device_stats(room, day);
  assert aggregate.active_seconds = 2 and aggregate.answer_count = 2;

  perform public.sync_review_day(
    room, '1700000000000', day,
    jsonb_build_array(jsonb_build_object(
      'id', review_a, 'card_id', '101', 'time_ms', 2500,
      'changed_at', 200
    )), '[]'::jsonb);
  perform public.sync_review_day(
    room, '1700000000000', day, '[]'::jsonb,
    jsonb_build_array(jsonb_build_object(
      'id', review_a, 'card_id', '101', 'changed_at', 100
    )));
  select * into aggregate from public.get_group_device_stats(room, day);
  assert aggregate.active_seconds = 5 and aggregate.answer_count = 3;

  -- An empty native marker intentionally replaces legacy totals with zero.
  perform public.record_device_day(
    room, device, day + 1, 1, 90, 4, 'stopped', 30, 20);
  perform public.sync_review_day(
    room, '1700000000000', day + 1, '[]'::jsonb, '[]'::jsonb);
  select * into aggregate from public.get_group_device_stats(room, day + 1);
  assert aggregate.active_seconds = 0 and aggregate.answer_count = 0;
  assert aggregate.time_goal_minutes = 30 and aggregate.card_goal = 20;

  begin
    perform public.sync_review_day(
      room, '1700000000000', day,
      jsonb_build_array(jsonb_build_object(
        'id', (base_ms - 1)::text, 'card_id', '104', 'time_ms', 1000
      )), '[]'::jsonb);
    raise exception 'out-of-day review accepted';
  exception when raise_exception then
    if sqlerrm <> 'invalid review event' then raise; end if;
  end;

  begin
    perform public.sync_review_day(
      room, 'bad collection!', day, '[]'::jsonb, '[]'::jsonb);
    raise exception 'invalid collection accepted';
  exception when raise_exception then
    if sqlerrm <> 'invalid review batch' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', stranger::text, true);
  begin
    perform public.sync_review_day(
      room, '1700000000000', day, '[]'::jsonb, '[]'::jsonb);
    raise exception 'nonmember accepted';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform public.sync_review_day(
      room, '1700000000000', day, '[]'::jsonb, '[]'::jsonb);
    raise exception 'unauthenticated caller accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;

  assert not has_table_privilege(
    'authenticated', 'public.anki_review_events', 'SELECT');
  assert not has_table_privilege(
    'authenticated', 'public.anki_review_events', 'INSERT');
  assert not has_table_privilege(
    'authenticated', 'public.anki_review_day_markers', 'SELECT');
  assert not has_function_privilege(
    'anon', 'public.sync_review_day(uuid,text,date,jsonb,jsonb)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.sync_review_day(uuid,text,date,jsonb,jsonb)', 'EXECUTE');

  perform set_config('request.jwt.claim.sub', person::text, true);
  perform public.leave_study_group(room);
  assert not exists (
    select 1 from public.anki_review_day_markers where group_id = room);
  assert not exists (
    select 1 from public.anki_review_events where group_id = room);
end;
$$;
rollback;
