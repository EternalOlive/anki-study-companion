-- Run after schema.sql and 20261003_activate_device_sync.sql on a TEST database.
-- All fixtures and changes are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  person uuid := gen_random_uuid();
  stranger uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  device_a uuid := gen_random_uuid();
  device_b uuid := gen_random_uuid();
  stored public.device_daily_stats;
  aggregate record;
  first_receipt timestamptz;
begin
  insert into auth.users(id) values (person), (stranger);
  insert into public.study_groups(id, name, owner_id)
    values (room, 'device sync regression', person);
  insert into public.group_members(group_id, user_id) values (room, person);
  perform set_config('request.jwt.claim.sub', person::text, true);

  select * into stored from public.record_device_day(
    room, device_a, current_date, 2, 120, 8, 'studying', 60, 100);
  assert stored.user_id = person and stored.active_seconds = 120;
  update public.device_daily_stats set updated_at = now() - interval '60 seconds'
    where group_id = room and device_id = device_a
    returning updated_at into first_receipt;

  select * into stored from public.record_device_day(
    room, device_a, current_date, 2, 999, 99, 'stopped', 1, 1);
  assert stored.active_seconds = 120 and stored.answer_count = 8;
  assert stored.status = 'studying' and stored.updated_at = first_receipt;
  select * into stored from public.record_device_day(
    room, device_a, current_date, 1, 60, 3, 'paused', 1, 1);
  assert stored.revision = 2 and stored.status = 'studying';

  select * into stored from public.record_device_day(
    room, device_a, current_date, 3, 100, 7, 'paused', 45, 80);
  assert stored.revision = 3 and stored.active_seconds = 120;
  assert stored.answer_count = 8 and stored.status = 'paused';
  assert stored.time_goal_minutes = 45 and stored.card_goal = 80;

  update public.device_daily_stats set updated_at = now() - interval '30 seconds'
    where group_id = room and device_id = device_a;

  perform public.record_device_day(
    room, device_b, current_date, 1, 60, 4, 'studying', 75, 120);
  select * into aggregate from public.get_group_device_stats(room, current_date);
  assert aggregate.active_seconds = 180 and aggregate.answer_count = 12;
  assert aggregate.time_goal_minutes = 75 and aggregate.card_goal = 120;
  assert aggregate.status = 'studying';
  assert not exists (select 1 from public.daily_stats where group_id = room);

  update public.device_daily_stats set updated_at = now() - interval '2 minutes'
    where group_id = room and device_id = device_b;
  select * into aggregate from public.get_group_device_stats(room, current_date);
  assert aggregate.status = 'paused';

  begin
    perform public.record_device_day(
      room, device_a, current_date, 4, -1, 0, 'paused', 0, 0);
    raise exception 'negative counter accepted';
  exception when raise_exception then
    if sqlerrm <> 'invalid device snapshot' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', stranger::text, true);
  begin
    perform public.record_device_day(
      room, device_a, current_date, 4, 200, 10, 'studying', 0, 0);
    raise exception 'nonmember accepted';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;
  begin
    perform public.get_group_device_stats(room, current_date);
    raise exception 'nonmember read accepted';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform public.record_device_day(
      room, device_a, current_date, 4, 200, 10, 'studying', 0, 0);
    raise exception 'unauthenticated caller accepted';
  exception when raise_exception then
    if sqlerrm <> 'authentication required' then raise; end if;
  end;

  assert not has_table_privilege('authenticated', 'public.device_daily_stats', 'INSERT');
  assert not has_table_privilege('authenticated', 'public.device_daily_stats', 'UPDATE');
  assert not has_table_privilege('anon', 'public.device_daily_stats', 'SELECT');
  assert not has_table_privilege('authenticated', 'public.daily_stats', 'INSERT');
  assert not has_table_privilege('authenticated', 'public.daily_stats', 'UPDATE');
  assert not has_function_privilege('anon',
    'public.record_device_day(uuid,uuid,date,bigint,bigint,bigint,text,integer,integer)',
    'EXECUTE');
  assert not has_function_privilege('anon',
    'public.get_group_device_stats(uuid,date)', 'EXECUTE');

  perform set_config('request.jwt.claim.sub', person::text, true);
  perform public.leave_study_group(room);
  assert not exists (select 1 from public.device_daily_stats where group_id = room);
end;
$$;
rollback;
