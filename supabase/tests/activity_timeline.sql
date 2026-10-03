-- Run after schema.sql, synced_review_totals and activity_timeline.
-- All fixtures and changes are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  person uuid := gen_random_uuid();
  empty_person uuid := gen_random_uuid();
  unknown_person uuid := gen_random_uuid();
  stranger uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  day date := (now() at time zone 'Asia/Seoul')::date;
  base_ms bigint;
  timeline record;
  buckets jsonb;
begin
  base_ms := (
    extract(epoch from (day::timestamp at time zone 'Asia/Seoul')) * 1000
  )::bigint;

  insert into auth.users(id)
    values (person), (empty_person), (unknown_person), (stranger);
  insert into public.study_groups(id, name, owner_id)
    values (room, 'activity timeline regression', person);
  insert into public.group_members(group_id, user_id)
    values (room, person), (room, empty_person), (room, unknown_person);

  perform set_config('request.jwt.claim.sub', person::text, true);
  perform public.sync_review_day(
    room, '1700000000000', day,
    jsonb_build_array(
      jsonb_build_object(
        'id', (base_ms + 1000)::text,
        'card_id', '101', 'time_ms', 1000, 'changed_at', 10
      ),
      jsonb_build_object(
        'id', (base_ms + 899000)::text,
        'card_id', '102', 'time_ms', 2000, 'changed_at', 10
      ),
      jsonb_build_object(
        'id', (base_ms + 900000)::text,
        'card_id', '103', 'time_ms', 3000, 'changed_at', 10
      ),
      jsonb_build_object(
        'id', (base_ms + 86399000)::text,
        'card_id', '104', 'time_ms', 4000, 'changed_at', 10
      )
    ), '[]'::jsonb
  );

  -- A stale duplicate cannot alter the original event.
  perform public.sync_review_day(
    room, '1700000000000', day,
    jsonb_build_array(jsonb_build_object(
      'id', (base_ms + 1000)::text,
      'card_id', '101', 'time_ms', 999999, 'changed_at', 5
    )), '[]'::jsonb
  );

  -- A deletion removes only that event from its quarter-hour bucket.
  perform public.sync_review_day(
    room, '1700000000000', day, '[]'::jsonb,
    jsonb_build_array(jsonb_build_object(
      'id', (base_ms + 899000)::text,
      'card_id', '102', 'changed_at', 20
    ))
  );

  perform set_config('request.jwt.claim.sub', empty_person::text, true);
  perform public.sync_review_day(
    room, '1800000000000', day, '[]'::jsonb, '[]'::jsonb
  );

  perform set_config('request.jwt.claim.sub', person::text, true);
  select * into timeline
  from public.get_group_activity_timeline(room, day)
  where user_id = person;
  assert timeline.activity_known;
  buckets := timeline.activity_buckets;
  assert jsonb_array_length(buckets) = 3;
  assert buckets->0 = jsonb_build_object(
    'slot', 0, 'answer_count', 1, 'time_ms', 1000
  );
  assert buckets->1 = jsonb_build_object(
    'slot', 1, 'answer_count', 1, 'time_ms', 3000
  );
  assert buckets->2 = jsonb_build_object(
    'slot', 95, 'answer_count', 1, 'time_ms', 4000
  );

  select * into timeline
  from public.get_group_activity_timeline(room, day)
  where user_id = empty_person;
  assert timeline.activity_known and timeline.activity_buckets = '[]'::jsonb;

  select * into timeline
  from public.get_group_activity_timeline(room, day)
  where user_id = unknown_person;
  assert not timeline.activity_known and timeline.activity_buckets = '[]'::jsonb;

  assert (
    select count(*) from public.get_group_activity_timeline(room, day)
  ) = 3;

  begin
    perform * from public.get_group_activity_timeline(room, null);
    raise exception 'null target day accepted';
  exception when raise_exception then
    if sqlerrm <> 'target day required' then raise; end if;
  end;

  begin
    perform * from public.get_group_activity_timeline(room, day - 1);
    raise exception 'past activity timeline accepted';
  exception when raise_exception then
    if sqlerrm <> 'only current day activity is available' then raise; end if;
  end;

  begin
    perform * from public.get_group_activity_timeline(room, day + 1);
    raise exception 'future activity timeline accepted';
  exception when raise_exception then
    if sqlerrm <> 'only current day activity is available' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', stranger::text, true);
  begin
    perform * from public.get_group_activity_timeline(room, day);
    raise exception 'nonmember activity timeline accepted';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform * from public.get_group_activity_timeline(room, day);
    raise exception 'unauthenticated activity timeline accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;

  assert not has_function_privilege(
    'anon', 'public.get_group_activity_timeline(uuid,date)', 'EXECUTE'
  );
  assert has_function_privilege(
    'authenticated', 'public.get_group_activity_timeline(uuid,date)', 'EXECUTE'
  );
end;
$$;
rollback;
