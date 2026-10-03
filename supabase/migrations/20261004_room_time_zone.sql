-- Give every room one immutable IANA time zone and use Anki's 04:00 day
-- boundary. Retained raw events are relabelled transactionally from their
-- timestamps; frozen archives retain their historical legacy labels.
begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

alter table public.study_groups
  add column if not exists time_zone text not null default 'Asia/Seoul',
  add column if not exists day_start_hour smallint not null default 4;

alter table public.study_groups
  drop constraint if exists study_groups_day_start_hour_check;
alter table public.study_groups
  add constraint study_groups_day_start_hour_check
  check (day_start_hour = 4);

-- Create every destination marker before changing the FK-bearing event row.
-- A raw event can never be merged into an already frozen destination day.
lock table public.anki_review_day_markers in share row exclusive mode;
lock table public.anki_review_events in share row exclusive mode;
do $$
declare
  before_count bigint;
  after_count bigint;
  before_time numeric;
  after_time numeric;
begin
  select count(*), coalesce(sum(e.time_ms), 0)
  into before_count, before_time from public.anki_review_events e;

  if exists (
    select 1
    from public.anki_review_events e
    join public.study_groups sg on sg.id = e.group_id
    join public.anki_review_day_markers destination
      on destination.group_id = e.group_id
      and destination.user_id = e.user_id
      and destination.collection_id = e.collection_id
      and destination.study_day = (
        (to_timestamp(e.review_id::numeric / 1000.0)
          at time zone sg.time_zone)
        - make_interval(hours => sg.day_start_hour)
      )::date
    where destination.archived_at is not null
      and e.study_day is distinct from destination.study_day
  ) then
    raise exception 'raw review conflicts with archived room day';
  end if;

  insert into public.anki_review_day_markers as destination (
    group_id, user_id, collection_id, study_day, updated_at
  )
  select e.group_id, e.user_id, e.collection_id,
    ((to_timestamp(e.review_id::numeric / 1000.0)
      at time zone sg.time_zone)
      - make_interval(hours => sg.day_start_hour))::date,
    max(source.updated_at)
  from public.anki_review_events e
  join public.study_groups sg on sg.id = e.group_id
  join public.anki_review_day_markers source
    on source.group_id = e.group_id and source.user_id = e.user_id
    and source.collection_id = e.collection_id
    and source.study_day = e.study_day
  group by e.group_id, e.user_id, e.collection_id,
    ((to_timestamp(e.review_id::numeric / 1000.0)
      at time zone sg.time_zone)
      - make_interval(hours => sg.day_start_hour))::date
  on conflict (group_id, user_id, collection_id, study_day) do update
  set updated_at = greatest(destination.updated_at, excluded.updated_at);

  update public.anki_review_events e
  set study_day = (
    (to_timestamp(e.review_id::numeric / 1000.0)
      at time zone sg.time_zone)
    - make_interval(hours => sg.day_start_hour)
  )::date
  from public.study_groups sg
  where sg.id = e.group_id and e.study_day is distinct from (
    (to_timestamp(e.review_id::numeric / 1000.0)
      at time zone sg.time_zone)
    - make_interval(hours => sg.day_start_hour)
  )::date;

  select count(*), coalesce(sum(e.time_ms), 0)
  into after_count, after_time from public.anki_review_events e;
  if before_count <> after_count or before_time <> after_time then
    raise exception 'review reclassification invariant failed';
  end if;
end;
$$;

create or replace function public.guard_study_group_calendar()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.time_zone := btrim(new.time_zone);
  if new.time_zone is null or new.time_zone = '' or not exists (
    select 1 from pg_catalog.pg_timezone_names zones
    where zones.name = new.time_zone
  ) then
    raise exception 'invalid room time zone';
  end if;
  if new.day_start_hour <> 4 then
    raise exception 'invalid room day boundary';
  end if;
  if tg_op = 'UPDATE' and (
    new.time_zone is distinct from old.time_zone
    or new.day_start_hour is distinct from old.day_start_hour
  ) then
    raise exception 'room calendar is immutable';
  end if;
  return new;
end;
$$;

drop trigger if exists study_groups_calendar_guard on public.study_groups;
create trigger study_groups_calendar_guard
before insert or update on public.study_groups
for each row execute function public.guard_study_group_calendar();

revoke all on function public.guard_study_group_calendar()
  from public, anon, authenticated;

drop function if exists public.create_study_group(text);
create or replace function public.create_study_group(
  group_name text,
  room_timezone text default 'Asia/Seoul'
)
returns table (
  group_id uuid,
  invite_code text,
  time_zone text,
  day_start_hour smallint
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  normalized_timezone text := btrim(room_timezone);
  created_group public.study_groups;
  attempt integer;
begin
  if actor is null then raise exception 'authentication required'; end if;
  if group_name is null or char_length(btrim(group_name)) not between 1 and 50 then
    raise exception 'invalid group name';
  end if;
  if normalized_timezone is null or normalized_timezone = '' or not exists (
    select 1 from pg_catalog.pg_timezone_names zones
    where zones.name = normalized_timezone
  ) then
    raise exception 'invalid room time zone';
  end if;

  for attempt in 1..10 loop
    begin
      insert into public.study_groups(
        name, invite_code, owner_id, time_zone, day_start_hour
      ) values (
        btrim(group_name), public.generate_invite_code(), actor,
        normalized_timezone, 4
      ) returning * into created_group;
      exit;
    exception when unique_violation then
      if attempt = 10 then raise exception 'could not allocate invite code'; end if;
    end;
  end loop;

  insert into public.group_members(group_id, user_id)
  values (created_group.id, actor);

  return query select created_group.id, created_group.invite_code,
    created_group.time_zone, created_group.day_start_hour;
end;
$$;

revoke all on function public.create_study_group(text, text)
  from public, anon, authenticated;
grant execute on function public.create_study_group(text, text)
  to authenticated;

create or replace function public.join_study_group_safe(code text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  target_group public.study_groups%rowtype;
  rate_record public.invite_join_rate_limits%rowtype;
  retry_after integer;
begin
  if actor is null then raise exception 'authentication required'; end if;

  insert into public.invite_join_rate_limits as limits (
    user_id, window_started_at, attempt_count
  ) values (actor, now(), 1)
  on conflict (user_id) do update set
    window_started_at = case
      when limits.window_started_at <= now() - interval '10 minutes' then now()
      else limits.window_started_at end,
    attempt_count = case
      when limits.window_started_at <= now() - interval '10 minutes' then 1
      else limits.attempt_count + 1 end
  returning * into rate_record;

  if rate_record.attempt_count > 8 then
    retry_after := greatest(1, ceil(extract(epoch from (
      rate_record.window_started_at + interval '10 minutes' - now()
    )))::integer);
    return jsonb_build_object('ok', false, 'error', 'TOO_MANY_ATTEMPTS',
      'retry_after', retry_after);
  end if;
  if code is null or upper(btrim(code)) !~ '^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}$' then
    return jsonb_build_object('ok', false, 'error', 'INVALID_INVITE_CODE',
      'retry_after', 0);
  end if;

  select sg.* into target_group from public.study_groups sg
  where sg.invite_code = upper(btrim(code)) for key share;
  if target_group.id is null then
    return jsonb_build_object('ok', false, 'error', 'INVALID_INVITE_CODE',
      'retry_after', 0);
  end if;

  insert into public.group_members(group_id, user_id)
  values (target_group.id, actor) on conflict do nothing;

  return jsonb_build_object(
    'ok', true,
    'group_id', target_group.id,
    'time_zone', target_group.time_zone,
    'day_start_hour', target_group.day_start_hour
  );
end;
$$;

revoke all on function public.join_study_group_safe(text)
  from public, anon, authenticated;
grant execute on function public.join_study_group_safe(text) to authenticated;

create or replace function public.sync_review_day(
  target_group uuid,
  source_collection text,
  target_day date,
  reviews jsonb,
  removed_ids jsonb default '[]'::jsonb
)
returns table (
  study_day date,
  active_review_count bigint,
  active_time_ms bigint,
  updated_at timestamptz
)
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  actor uuid := auth.uid();
  room public.study_groups%rowtype;
  item jsonb;
  item_review_id text;
  item_card_id text;
  item_time_text text;
  item_change_text text;
  event_at timestamptz;
  room_lower timestamptz;
  room_upper timestamptz;
  marker_archived_at timestamptz;
begin
  if actor is null then raise exception 'authentication required'; end if;
  select sg.* into room from public.study_groups sg
  join public.group_members gm on gm.group_id = sg.id
  where sg.id = target_group and gm.user_id = actor for key share of sg;
  if room.id is null then raise exception 'not a member of this group'; end if;

  if source_collection is null
    or source_collection <> btrim(source_collection)
    or length(source_collection) not between 1 and 128
    or source_collection !~ '^[A-Za-z0-9._:-]+$'
    or target_day is null
    or reviews is null or jsonb_typeof(reviews) <> 'array'
    or removed_ids is null or jsonb_typeof(removed_ids) <> 'array'
    or jsonb_array_length(reviews) + jsonb_array_length(removed_ids) > 1000 then
    raise exception 'invalid review batch';
  end if;

  room_lower := (target_day + make_interval(hours => room.day_start_hour))
    at time zone room.time_zone;
  room_upper := (target_day + 1 + make_interval(hours => room.day_start_hour))
    at time zone room.time_zone;

  insert into public.anki_review_day_markers (
    group_id, user_id, collection_id, study_day
  ) values (target_group, actor, source_collection, target_day)
  on conflict on constraint anki_review_day_markers_pkey do nothing;

  select m.archived_at into marker_archived_at
  from public.anki_review_day_markers m
  where m.group_id = target_group and m.user_id = actor
    and m.collection_id = source_collection and m.study_day = target_day
  for update;
  if marker_archived_at is not null then raise exception 'review day archived'; end if;

  update public.anki_review_day_markers m set updated_at = now()
  where m.group_id = target_group and m.user_id = actor
    and m.collection_id = source_collection and m.study_day = target_day;

  for item in select value from jsonb_array_elements(reviews) loop
    item_review_id := item->>'id';
    item_card_id := item->>'card_id';
    item_time_text := item->>'time_ms';
    item_change_text := coalesce(item->>'changed_at', '0');
    if jsonb_typeof(item) is distinct from 'object'
      or jsonb_typeof(item->'id') is distinct from 'string'
      or jsonb_typeof(item->'card_id') is distinct from 'string'
      or jsonb_typeof(item->'time_ms') is distinct from 'number'
      or (item ? 'changed_at' and jsonb_typeof(item->'changed_at') is distinct from 'number')
      or item_review_id !~ '^[0-9]{10,16}$'
      or item_card_id !~ '^[0-9]{1,20}$'
      or item_time_text !~ '^[0-9]{1,7}$'
      or item_change_text !~ '^[0-9]{1,18}$' then
      raise exception 'invalid review event';
    end if;
    event_at := to_timestamp(item_review_id::numeric / 1000.0);
    if item_card_id::numeric <= 0 or item_time_text::numeric > 3600000
      or event_at < room_lower or event_at >= room_upper then
      raise exception 'invalid review event';
    end if;

    insert into public.anki_review_events as current (
      group_id, user_id, collection_id, study_day,
      review_id, card_id, time_ms, change_version
    ) values (
      target_group, actor, source_collection, target_day,
      item_review_id, item_card_id, item_time_text::integer,
      item_change_text::bigint
    ) on conflict (group_id, user_id, collection_id, review_id, card_id)
    do update set
      study_day = excluded.study_day,
      time_ms = case when excluded.change_version > current.change_version
        then excluded.time_ms else current.time_ms end,
      change_version = greatest(excluded.change_version, current.change_version),
      deleted = case when excluded.change_version > current.change_version
        then false else current.deleted end,
      deleted_at = case when excluded.change_version > current.change_version
        then null else current.deleted_at end
    where excluded.change_version > current.change_version
      or excluded.study_day is distinct from current.study_day;
  end loop;

  for item in select value from jsonb_array_elements(removed_ids) loop
    item_review_id := item->>'id';
    item_card_id := item->>'card_id';
    item_change_text := coalesce(item->>'changed_at', '0');
    if jsonb_typeof(item) is distinct from 'object'
      or jsonb_typeof(item->'id') is distinct from 'string'
      or jsonb_typeof(item->'card_id') is distinct from 'string'
      or (item ? 'changed_at' and jsonb_typeof(item->'changed_at') is distinct from 'number')
      or item_review_id !~ '^[0-9]{10,16}$'
      or item_card_id !~ '^[0-9]{1,20}$'
      or item_change_text !~ '^[0-9]{1,18}$' then
      raise exception 'invalid removed review';
    end if;
    event_at := to_timestamp(item_review_id::numeric / 1000.0);
    if item_card_id::numeric <= 0
      or event_at < room_lower or event_at >= room_upper then
      raise exception 'invalid removed review';
    end if;

    insert into public.anki_review_events as current (
      group_id, user_id, collection_id, study_day,
      review_id, card_id, time_ms, change_version, deleted, deleted_at
    ) values (
      target_group, actor, source_collection, target_day,
      item_review_id, item_card_id, 0, item_change_text::bigint, true, now()
    ) on conflict (group_id, user_id, collection_id, review_id, card_id)
    do update set
      study_day = excluded.study_day,
      change_version = greatest(excluded.change_version, current.change_version),
      deleted = case when excluded.change_version > current.change_version
        then true else current.deleted end,
      deleted_at = case when excluded.change_version > current.change_version
        then now() else current.deleted_at end
    where excluded.change_version > current.change_version
      or excluded.study_day is distinct from current.study_day;
  end loop;

  return query
  select target_day,
    (count(e.review_id) filter (where not e.deleted))::bigint,
    coalesce(sum(e.time_ms) filter (where not e.deleted), 0)::bigint,
    m.updated_at
  from public.anki_review_day_markers m
  left join public.anki_review_events e
    on e.group_id = m.group_id and e.user_id = m.user_id
    and e.collection_id = m.collection_id and e.study_day = m.study_day
  where m.group_id = target_group and m.user_id = actor
    and m.collection_id = source_collection and m.study_day = target_day
  group by m.updated_at;
end;
$$;

create or replace function public.get_group_activity_timeline(
  target_group uuid,
  target_day date
)
returns table (
  user_id uuid,
  study_day date,
  activity_buckets jsonb,
  activity_known boolean
)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  room public.study_groups%rowtype;
  current_room_day date;
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_day is null then raise exception 'target day required'; end if;
  select sg.* into room from public.study_groups sg
  join public.group_members gm on gm.group_id = sg.id
  where sg.id = target_group and gm.user_id = actor;
  if room.id is null then raise exception 'not a member of this group'; end if;
  current_room_day := ((now() at time zone room.time_zone)
    - make_interval(hours => room.day_start_hour))::date;
  if target_day <> current_room_day then
    raise exception 'only current day activity is available';
  end if;

  return query
  with room_members as (
    select gm.user_id from public.group_members gm
    where gm.group_id = target_group
  ), known_days as (
    select m.user_id, true as activity_known
    from public.anki_review_day_markers m
    where m.group_id = target_group and m.study_day = target_day
    group by m.user_id
  ), bucket_totals as (
    select e.user_id,
      mod(
        extract(hour from (to_timestamp(e.review_id::numeric / 1000.0)
          at time zone room.time_zone))::integer * 4
        + floor(extract(minute from (to_timestamp(e.review_id::numeric / 1000.0)
          at time zone room.time_zone)) / 15)::integer
        - room.day_start_hour * 4 + 96,
        96
      ) as slot,
      count(*)::integer as answer_count,
      coalesce(sum(e.time_ms), 0)::bigint as time_ms
    from public.anki_review_events e
    where e.group_id = target_group and e.study_day = target_day and not e.deleted
    group by e.user_id, slot
  ), bucket_arrays as (
    select b.user_id, jsonb_agg(jsonb_build_object(
      'slot', b.slot, 'answer_count', b.answer_count, 'time_ms', b.time_ms
    ) order by b.slot) as activity_buckets
    from bucket_totals b group by b.user_id
  )
  select rm.user_id, target_day,
    coalesce(ba.activity_buckets, '[]'::jsonb),
    coalesce(kd.activity_known, false)
  from room_members rm
  left join known_days kd using (user_id)
  left join bucket_arrays ba using (user_id)
  order by rm.user_id;
end;
$$;

-- Device rows supply goals and presence only. Synced review rows remain the
-- sole source for time/count totals, avoiding midnight-based legacy counters.
create or replace function public.get_group_device_stats(
  target_group uuid,
  target_day date
)
returns table (
  group_id uuid,
  user_id uuid,
  study_day date,
  active_seconds bigint,
  answer_count bigint,
  time_goal_minutes integer,
  card_goal integer,
  status text,
  updated_at timestamptz
)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_day is null then raise exception 'target day required'; end if;
  if not exists (
    select 1 from public.group_members gm
    where gm.group_id = target_group and gm.user_id = actor
  ) then raise exception 'not a member of this group'; end if;

  return query
  with device_rows as (
    select d.* from public.device_daily_stats d
    where d.group_id = target_group and d.study_day = target_day
  ), raw_collection_totals as (
    select e.user_id, e.collection_id,
      (count(e.review_id) filter (where not e.deleted))::bigint as review_count,
      coalesce(sum(e.time_ms) filter (where not e.deleted), 0)::bigint as review_time,
      max(e.created_at) as last_event_at
    from public.anki_review_events e
    where e.group_id = target_group and e.study_day = target_day
    group by e.user_id, e.collection_id
  ), native_collection_totals as (
    select m.user_id, m.collection_id,
      case when m.archived_at is not null
        then coalesce(a.active_review_count, 0)
        else coalesce(r.review_count, 0) end as review_count,
      case when m.archived_at is not null
        then coalesce(a.active_time_ms, 0)
        else coalesce(r.review_time, 0) end as review_time,
      greatest(m.updated_at, coalesce(r.last_event_at, m.updated_at)) as updated_at
    from public.anki_review_day_markers m
    left join raw_collection_totals r
      on r.user_id = m.user_id and r.collection_id = m.collection_id
    left join public.anki_review_day_archives a
      on a.group_id = m.group_id and a.user_id = m.user_id
      and a.collection_id = m.collection_id and a.study_day = m.study_day
    where m.group_id = target_group and m.study_day = target_day
  ), native_totals as (
    select n.user_id,
      (sum(n.review_time) / 1000)::bigint as active_seconds,
      sum(n.review_count)::bigint as answer_count,
      max(n.updated_at) as updated_at
    from native_collection_totals n group by n.user_id
  ), latest_device as (
    select distinct on (d.user_id) d.user_id, d.time_goal_minutes,
      d.card_goal, d.updated_at
    from device_rows d order by d.user_id, d.updated_at desc, d.device_id
  ), users_for_day as (
    select n.user_id from native_totals n
    union select d.user_id from latest_device d
  ), presence as (
    select u.user_id,
      case when exists (
        select 1 from device_rows d where d.user_id = u.user_id
          and d.status = 'studying'
          and d.updated_at >= now() - interval '90 seconds'
      ) then 'studying'
      else coalesce((
        select d.status from device_rows d where d.user_id = u.user_id
          and d.status in ('paused', 'stopped')
          and d.updated_at >= now() - interval '90 seconds'
        order by d.updated_at desc, d.device_id limit 1
      ), 'stopped') end as status
    from users_for_day u
  )
  select target_group, u.user_id, target_day,
    coalesce(n.active_seconds, 0)::bigint,
    coalesce(n.answer_count, 0)::bigint,
    coalesce(d.time_goal_minutes, 0), coalesce(d.card_goal, 0),
    p.status, greatest(n.updated_at, d.updated_at)
  from users_for_day u
  left join native_totals n using (user_id)
  left join latest_device d using (user_id)
  join presence p using (user_id)
  order by greatest(n.updated_at, d.updated_at) desc nulls last, u.user_id;
end;
$$;

-- Retention is evaluated against each room's current 04:00-based study day.
create or replace function public.archive_review_days(
  retain_days integer default 90,
  max_days integer default 500
)
returns table (archived_day_count bigint, deleted_event_count bigint)
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  marker record;
  archived_total bigint := 0;
  deleted_total bigint := 0;
  deleted_for_day bigint;
  frozen_at timestamptz;
begin
  if retain_days is null or retain_days < 90 or retain_days > 3650
    or max_days is null or max_days < 1 or max_days > 5000 then
    raise exception 'invalid retention request';
  end if;

  for marker in
    select m.group_id, m.user_id, m.collection_id, m.study_day
    from public.anki_review_day_markers m
    join public.study_groups sg on sg.id = m.group_id
    where m.archived_at is null and m.study_day < (
      ((now() at time zone sg.time_zone)
        - make_interval(hours => sg.day_start_hour))::date - (retain_days - 1)
    )
    order by m.study_day, m.group_id, m.user_id, m.collection_id
    limit max_days for update of m skip locked
  loop
    frozen_at := clock_timestamp();
    insert into public.anki_review_day_archives (
      group_id, user_id, collection_id, study_day,
      active_review_count, active_time_ms, archived_at
    )
    select marker.group_id, marker.user_id, marker.collection_id,
      marker.study_day,
      (count(e.review_id) filter (where not e.deleted))::bigint,
      coalesce(sum(e.time_ms) filter (where not e.deleted), 0)::bigint,
      frozen_at
    from public.anki_review_events e
    where e.group_id = marker.group_id and e.user_id = marker.user_id
      and e.collection_id = marker.collection_id and e.study_day = marker.study_day
    on conflict (group_id, user_id, collection_id, study_day) do nothing;
    if not found then raise exception 'review archive conflict'; end if;

    update public.anki_review_day_markers m set archived_at = frozen_at
    where m.group_id = marker.group_id and m.user_id = marker.user_id
      and m.collection_id = marker.collection_id
      and m.study_day = marker.study_day and m.archived_at is null;
    if not found then raise exception 'review archive lock lost'; end if;

    delete from public.anki_review_events e
    where e.group_id = marker.group_id and e.user_id = marker.user_id
      and e.collection_id = marker.collection_id and e.study_day = marker.study_day;
    get diagnostics deleted_for_day = row_count;
    archived_total := archived_total + 1;
    deleted_total := deleted_total + deleted_for_day;
  end loop;
  return query select archived_total, deleted_total;
end;
$$;

revoke all on function public.sync_review_day(uuid, text, date, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.sync_review_day(uuid, text, date, jsonb, jsonb)
  to authenticated;
revoke all on function public.get_group_activity_timeline(uuid, date)
  from public, anon, authenticated;
grant execute on function public.get_group_activity_timeline(uuid, date)
  to authenticated;
revoke all on function public.get_group_device_stats(uuid, date)
  from public, anon, authenticated;
grant execute on function public.get_group_device_stats(uuid, date)
  to authenticated;
revoke all on function public.archive_review_days(integer, integer)
  from public, anon, authenticated;
grant execute on function public.archive_review_days(integer, integer)
  to service_role;

notify pgrst, 'reload schema';

commit;
