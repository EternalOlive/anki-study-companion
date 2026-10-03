-- Run after schema.sql, activate_device_sync, synced_review_totals and
-- review_retention. All fixtures and changes are rolled back.
begin;
do $$
declare
  person uuid := gen_random_uuid();
  stranger uuid := gen_random_uuid();
  room uuid := gen_random_uuid();
  old_day date := (now() at time zone 'Asia/Seoul')::date - 100;
  older_day date := (now() at time zone 'Asia/Seoul')::date - 101;
  first_expired_day date := (now() at time zone 'Asia/Seoul')::date - 90;
  boundary_day date := (now() at time zone 'Asia/Seoul')::date - 89;
  old_base bigint;
  older_base bigint;
  first_expired_base bigint;
  boundary_base bigint;
  receipt record;
  aggregate record;
  maintenance record;
  archived_count bigint;
begin
  old_base := (extract(epoch from (
    old_day::timestamp at time zone 'Asia/Seoul'
  )) * 1000)::bigint;
  older_base := (extract(epoch from (
    older_day::timestamp at time zone 'Asia/Seoul'
  )) * 1000)::bigint;
  first_expired_base := (extract(epoch from (
    first_expired_day::timestamp at time zone 'Asia/Seoul'
  )) * 1000)::bigint;
  boundary_base := (extract(epoch from (
    boundary_day::timestamp at time zone 'Asia/Seoul'
  )) * 1000)::bigint;

  insert into auth.users(id) values (person), (stranger);
  insert into public.study_groups(id, name, owner_id)
    values (room, 'review retention regression', person);
  insert into public.group_members(group_id, user_id) values (room, person);
  perform set_config('request.jwt.claim.sub', person::text, true);

  -- Two collections on one old day must remain separate in storage and add up
  -- exactly once in the existing daily-stat response.
  perform public.sync_review_day(
    room, 'collection-a', old_day,
    jsonb_build_array(
      jsonb_build_object(
        'id', (old_base + 1000)::text, 'card_id', '101', 'time_ms', 1000
      ),
      jsonb_build_object(
        'id', (old_base + 2000)::text, 'card_id', '102', 'time_ms', 2000
      )
    ), '[]'::jsonb);
  perform public.sync_review_day(
    room, 'collection-b', old_day,
    jsonb_build_array(jsonb_build_object(
      'id', (old_base + 3000)::text, 'card_id', '103', 'time_ms', 3000
    )), '[]'::jsonb);

  -- A tombstone contributes neither count nor time to the frozen total.
  perform public.sync_review_day(
    room, 'collection-a', old_day, '[]'::jsonb,
    jsonb_build_array(jsonb_build_object(
      'id', (old_base + 2000)::text, 'card_id', '102', 'changed_at', 100
    )));

  -- A still older empty marker verifies zero-day archival and the batch cap.
  perform public.sync_review_day(
    room, 'collection-z', older_day, '[]'::jsonb, '[]'::jsonb);

  -- The oldest eligible marker is processed first, and max_days is strict.
  select * into maintenance from public.archive_review_days(90, 1);
  assert maintenance.archived_day_count = 1;
  assert maintenance.deleted_event_count = 0;
  select count(*) into archived_count
  from public.anki_review_day_markers m where m.group_id = room
    and m.archived_at is not null;
  assert archived_count = 1;

  select * into maintenance from public.archive_review_days(90, 100);
  assert maintenance.archived_day_count = 2;
  assert maintenance.deleted_event_count = 3;

  assert not exists (
    select 1 from public.anki_review_events e
    where e.group_id = room and e.study_day in (old_day, older_day)
  );
  assert (
    select count(*) from public.anki_review_day_archives a
    where a.group_id = room and a.study_day = old_day
  ) = 2;
  assert (
    select sum(a.active_review_count)
    from public.anki_review_day_archives a
    where a.group_id = room and a.study_day = old_day
  ) = 2;
  assert (
    select sum(a.active_time_ms)
    from public.anki_review_day_archives a
    where a.group_id = room and a.study_day = old_day
  ) = 4000;

  select * into aggregate from public.get_group_device_stats(room, old_day);
  assert aggregate.answer_count = 2 and aggregate.active_seconds = 4;
  assert aggregate.status = 'stopped';

  -- Re-running maintenance does not double count or rewrite frozen totals.
  select * into maintenance from public.archive_review_days(90, 100);
  assert maintenance.archived_day_count = 0;
  assert maintenance.deleted_event_count = 0;
  select * into aggregate from public.get_group_device_stats(room, old_day);
  assert aggregate.answer_count = 2 and aggregate.active_seconds = 4;

  -- Stale upload and undo both fail explicitly after archival. No marker/event
  -- is silently acknowledged and no raw row can be resurrected.
  begin
    perform public.sync_review_day(
      room, 'collection-a', old_day,
      jsonb_build_array(jsonb_build_object(
        'id', (old_base + 1000)::text, 'card_id', '101', 'time_ms', 9999,
        'changed_at', 200
      )), '[]'::jsonb);
    raise exception 'archived replay accepted';
  exception when raise_exception then
    if sqlerrm <> 'review day archived' then raise; end if;
  end;
  begin
    perform public.sync_review_day(
      room, 'collection-a', old_day, '[]'::jsonb,
      jsonb_build_array(jsonb_build_object(
        'id', (old_base + 1000)::text, 'card_id', '101', 'changed_at', 300
      )));
    raise exception 'archived undo accepted';
  exception when raise_exception then
    if sqlerrm <> 'review day archived' then raise; end if;
  end;
  assert not exists (
    select 1 from public.anki_review_events e
    where e.group_id = room and e.study_day = old_day
  );

  -- Exactly 90 Asia/Seoul calendar days (today through day -89) stay raw and
  -- mutable. The receipt columns keep the old sync contract.
  select * into receipt from public.sync_review_day(
    room, 'collection-a', boundary_day,
    jsonb_build_array(jsonb_build_object(
      'id', (boundary_base + 1000)::text, 'card_id', '201', 'time_ms', 2500
    )), '[]'::jsonb);
  assert receipt.study_day = boundary_day;
  assert receipt.active_review_count = 1 and receipt.active_time_ms = 2500;
  assert receipt.updated_at is not null;
  select * into maintenance from public.archive_review_days(90, 100);
  assert maintenance.archived_day_count = 0;
  assert exists (
    select 1 from public.anki_review_events e
    where e.group_id = room and e.study_day = boundary_day
  );
  assert exists (
    select 1 from public.anki_review_day_markers m
    where m.group_id = room and m.study_day = boundary_day
      and m.archived_at is null
  );

  -- A first upload for a previously unseen old date is accepted honestly. It
  -- becomes immutable only after a later maintenance batch freezes it.
  select * into receipt from public.sync_review_day(
    room, 'late-first-upload', first_expired_day,
    jsonb_build_array(jsonb_build_object(
      'id', (first_expired_base + 4000)::text,
      'card_id', '301', 'time_ms', 500
    )), '[]'::jsonb);
  assert receipt.active_review_count = 1 and receipt.active_time_ms = 500;
  select * into maintenance from public.archive_review_days(90, 100);
  assert maintenance.archived_day_count = 1;
  assert maintenance.deleted_event_count = 1;

  -- The retention window cannot be shortened below 90 days and each run has a
  -- hard upper batch limit.
  begin
    perform public.archive_review_days(89, 100);
    raise exception 'short retention accepted';
  exception when raise_exception then
    if sqlerrm <> 'invalid retention request' then raise; end if;
  end;
  begin
    perform public.archive_review_days(90, 5001);
    raise exception 'oversized archive batch accepted';
  exception when raise_exception then
    if sqlerrm <> 'invalid retention request' then raise; end if;
  end;

  -- Raw and archive tables remain private. Only authenticated members may sync;
  -- only service_role may start maintenance.
  assert not has_table_privilege(
    'authenticated', 'public.anki_review_day_archives', 'SELECT');
  assert not has_table_privilege(
    'authenticated', 'public.anki_review_day_archives', 'INSERT');
  assert not has_function_privilege(
    'authenticated', 'public.archive_review_days(integer,integer)', 'EXECUTE');
  assert not has_function_privilege(
    'anon', 'public.archive_review_days(integer,integer)', 'EXECUTE');
  assert has_function_privilege(
    'service_role', 'public.archive_review_days(integer,integer)', 'EXECUTE');
  assert has_function_privilege(
    'authenticated', 'public.sync_review_day(uuid,text,date,jsonb,jsonb)',
    'EXECUTE');
  assert (
    select p.proconfig @> array['lock_timeout=5s']::text[]
    from pg_proc p
    where p.oid =
      'public.sync_review_day(uuid,text,date,jsonb,jsonb)'::regprocedure
  );
  assert (
    select p.proconfig @> array['lock_timeout=5s']::text[]
    from pg_proc p
    where p.oid =
      'public.archive_review_days(integer,integer)'::regprocedure
  );
  assert exists (
    select 1 from pg_indexes i
    where i.schemaname = 'public'
      and i.indexname = 'anki_review_day_markers_archive_candidates_idx'
      and i.indexdef ilike '%where (archived_at is null)%'
  );

  perform set_config('request.jwt.claim.sub', stranger::text, true);
  begin
    perform public.sync_review_day(
      room, 'collection-a', boundary_day, '[]'::jsonb, '[]'::jsonb);
    raise exception 'nonmember accepted';
  exception when raise_exception then
    if sqlerrm <> 'not a member of this group' then raise; end if;
  end;

  -- The archive FK follows the marker/member cascade on room departure.
  perform set_config('request.jwt.claim.sub', person::text, true);
  perform public.leave_study_group(room);
  assert not exists (
    select 1 from public.anki_review_day_archives where group_id = room);
  assert not exists (
    select 1 from public.anki_review_day_markers where group_id = room);
  assert not exists (
    select 1 from public.anki_review_events where group_id = room);

  -- True concurrent blocking/skip-locked behavior requires two connections.
  -- This file verifies the shared marker lock path transactionally; deployment
  -- verification must additionally race sync_review_day with maintenance.
  assert position(
    'for update' in lower(pg_get_functiondef(
      'public.sync_review_day(uuid,text,date,jsonb,jsonb)'::regprocedure
    ))
  ) > 0;
  assert position(
    'skip locked' in lower(pg_get_functiondef(
      'public.archive_review_days(integer,integer)'::regprocedure
    ))
  ) > 0;
end;
$$;
rollback;
