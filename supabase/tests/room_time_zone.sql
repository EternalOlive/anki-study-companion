-- Run after all 20261004 migrations, including room_time_zone.
-- All fixtures and changes are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  owner_user uuid := gen_random_uuid();
  joining_user uuid := gen_random_uuid();
  room uuid;
  default_room uuid;
  invite text;
  zone_name text;
  start_hour smallint;
  joined jsonb;
  day date;
  lower_at timestamptz;
  lower_ms bigint;
  upper_ms bigint;
  moved_review text;
  timeline record;
  aggregate record;
  device uuid := gen_random_uuid();
  spring_day_seconds bigint;
  fall_day_seconds bigint;
begin
  insert into auth.users(id) values (owner_user), (joining_user);
  perform set_config('request.jwt.claim.sub', owner_user::text, true);

  -- The old one-argument RPC call remains valid and defaults to Seoul.
  select c.group_id, c.time_zone, c.day_start_hour
  into default_room, zone_name, start_hour
  from public.create_study_group('default calendar') c;
  assert zone_name = 'Asia/Seoul' and start_hour = 4;

  select c.group_id, c.invite_code, c.time_zone, c.day_start_hour
  into room, invite, zone_name, start_hour
  from public.create_study_group(
    group_name => 'Auckland room',
    room_timezone => 'Pacific/Auckland'
  ) c;
  assert zone_name = 'Pacific/Auckland' and start_hour = 4;
  assert (select time_zone from public.study_groups where id = room)
    = 'Pacific/Auckland';

  begin
    perform * from public.create_study_group('bad zone', 'Mars/Olympus');
    raise exception 'invalid room time zone accepted';
  exception when raise_exception then
    if sqlerrm <> 'invalid room time zone' then raise; end if;
  end;
  begin
    update public.study_groups set time_zone = 'UTC' where id = room;
    raise exception 'room time zone changed';
  exception when raise_exception then
    if sqlerrm <> 'room calendar is immutable' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', joining_user::text, true);
  joined := public.join_study_group_safe(invite);
  assert joined->>'ok' = 'true';
  assert joined->>'group_id' = room::text;
  assert joined->>'time_zone' = 'Pacific/Auckland';
  assert joined->>'day_start_hour' = '4';

  -- Calendar-day bounds use regional UTC offsets, so DST transition days are
  -- 23/25 real hours while still running from local 04:00 to local 04:00.
  spring_day_seconds := extract(epoch from (
    ((date '2026-03-08' + time '04:00') at time zone 'America/New_York')
    - ((date '2026-03-07' + time '04:00') at time zone 'America/New_York')
  ))::bigint;
  fall_day_seconds := extract(epoch from (
    ((date '2026-11-01' + time '04:00') at time zone 'America/New_York')
    - ((date '2026-10-31' + time '04:00') at time zone 'America/New_York')
  ))::bigint;
  assert spring_day_seconds = 23 * 60 * 60;
  assert fall_day_seconds = 25 * 60 * 60;

  perform set_config('request.jwt.claim.sub', owner_user::text, true);
  day := ((now() at time zone 'Pacific/Auckland') - interval '4 hours')::date;
  lower_at := (day + time '04:00') at time zone 'Pacific/Auckland';
  lower_ms := (extract(epoch from lower_at) * 1000)::bigint;
  upper_ms := (extract(epoch from (
    (day + 1 + time '04:00') at time zone 'Pacific/Auckland'
  )) * 1000)::bigint;

  -- The room day is exactly local 04:00 through the next local 04:00.
  perform public.sync_review_day(
    room, 'calendar-test', day,
    jsonb_build_array(
      jsonb_build_object('id', lower_ms::text, 'card_id', '101',
        'time_ms', 1000, 'changed_at', 1),
      jsonb_build_object('id', (upper_ms - 1)::text, 'card_id', '102',
        'time_ms', 2000, 'changed_at', 1)
    ), '[]'::jsonb
  );
  begin
    perform public.sync_review_day(
      room, 'calendar-test', day,
      jsonb_build_array(jsonb_build_object(
        'id', (lower_ms - 1)::text, 'card_id', '103', 'time_ms', 1000
      )), '[]'::jsonb
    );
    raise exception 'pre-04 review accepted into next room day';
  exception when raise_exception then
    if sqlerrm <> 'invalid review event' then raise; end if;
  end;

  select * into timeline
  from public.get_group_activity_timeline(room, day)
  where user_id = owner_user;
  assert timeline.activity_known;
  assert timeline.activity_buckets->0 = jsonb_build_object(
    'slot', 0, 'answer_count', 1, 'time_ms', 1000
  );
  assert timeline.activity_buckets->1 = jsonb_build_object(
    'slot', 95, 'answer_count', 1, 'time_ms', 2000
  );

  -- Equal-version replay moves a legacy-labelled raw event without reviving or
  -- duplicating it. This is the client cache migration convergence path.
  moved_review := (lower_ms + 60000)::text;
  insert into public.anki_review_day_markers(
    group_id, user_id, collection_id, study_day
  ) values (room, owner_user, 'move-test', day + 1);
  insert into public.anki_review_events(
    group_id, user_id, collection_id, study_day, review_id, card_id,
    time_ms, change_version
  ) values (
    room, owner_user, 'move-test', day + 1, moved_review, '201', 500, 7
  );
  perform public.sync_review_day(
    room, 'move-test', day,
    jsonb_build_array(jsonb_build_object(
      'id', moved_review, 'card_id', '201', 'time_ms', 999,
      'changed_at', 7
    )), '[]'::jsonb
  );
  assert (
    select e.study_day = day and e.time_ms = 500
    from public.anki_review_events e
    where e.group_id = room and e.collection_id = 'move-test'
      and e.review_id = moved_review and e.card_id = '201'
  );
  assert (
    select count(*) from public.anki_review_events e
    where e.group_id = room and e.collection_id = 'move-test'
      and e.review_id = moved_review and e.card_id = '201'
  ) = 1;

  -- Device midnight totals no longer replace authoritative native review totals.
  insert into public.device_daily_stats(
    group_id, user_id, device_id, study_day, revision, active_seconds, answer_count,
    status, time_goal_minutes, card_goal
  ) values (room, joining_user, device, day, 1, 9999, 999, 'stopped', 60, 100);
  select * into aggregate from public.get_group_device_stats(room, day)
  where user_id = joining_user;
  assert aggregate.active_seconds = 0 and aggregate.answer_count = 0;
  assert aggregate.time_goal_minutes = 60 and aggregate.card_goal = 100;

  assert position(
    'sg.time_zone' in pg_get_functiondef(
      'public.archive_review_days(integer,integer)'::regprocedure
    )
  ) > 0;
  assert not has_function_privilege(
    'anon', 'public.create_study_group(text,text)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.create_study_group(text,text)', 'EXECUTE');
  assert not has_function_privilege(
    'authenticated', 'public.guard_study_group_calendar()', 'EXECUTE');
  assert not has_function_privilege(
    'anon', 'public.get_group_activity_timeline(uuid,date)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.get_group_activity_timeline(uuid,date)', 'EXECUTE');
end;
$$;
rollback;
