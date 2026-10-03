-- Use Anki's synced revlog rows as the authoritative daily totals.
-- Existing per-device rows remain responsible for goals and live presence.
begin;

create table if not exists public.anki_review_day_markers (
  group_id uuid not null,
  user_id uuid not null,
  collection_id text not null,
  study_day date not null,
  updated_at timestamptz not null default now(),
  primary key (group_id, user_id, collection_id, study_day),
  foreign key (group_id, user_id)
    references public.group_members(group_id, user_id) on delete cascade,
  check (length(collection_id) between 1 and 128)
);

create table if not exists public.anki_review_events (
  group_id uuid not null,
  user_id uuid not null,
  collection_id text not null,
  study_day date not null,
  review_id text not null,
  card_id text not null,
  time_ms integer not null check (time_ms between 0 and 3600000),
  change_version bigint not null default 0 check (change_version >= 0),
  deleted boolean not null default false,
  created_at timestamptz not null default now(),
  deleted_at timestamptz,
  primary key (
    group_id, user_id, collection_id, review_id, card_id
  ),
  foreign key (group_id, user_id, collection_id, study_day)
    references public.anki_review_day_markers(
      group_id, user_id, collection_id, study_day
    ) on delete cascade,
  check (length(collection_id) between 1 and 128),
  check (review_id ~ '^[0-9]{10,16}$'),
  check (card_id ~ '^[0-9]{1,20}$')
);

create index if not exists anki_review_day_markers_group_day_idx
  on public.anki_review_day_markers(group_id, study_day, user_id);
create index if not exists anki_review_events_group_day_active_idx
  on public.anki_review_events(group_id, study_day, user_id)
  where not deleted;

alter table public.anki_review_day_markers enable row level security;
alter table public.anki_review_events enable row level security;
revoke all on public.anki_review_day_markers from public, anon, authenticated;
revoke all on public.anki_review_events from public, anon, authenticated;

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
as $$
declare
  actor uuid := auth.uid();
  item jsonb;
  item_review_id text;
  item_card_id text;
  item_time_text text;
  item_change_text text;
  lower_ms numeric;
  upper_ms numeric;
begin
  if actor is null then raise exception 'authentication required'; end if;
  perform 1 from public.group_members
    where group_id = target_group and user_id = actor for key share;
  if not found then raise exception 'not a member of this group'; end if;

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

  lower_ms := extract(epoch from (target_day::timestamp at time zone 'Asia/Seoul')) * 1000;
  upper_ms := extract(epoch from ((target_day + 1)::timestamp at time zone 'Asia/Seoul')) * 1000;

  -- An empty batch still marks this user/day as migrated to native review totals.
  insert into public.anki_review_day_markers as current (
    group_id, user_id, collection_id, study_day
  ) values (
    target_group, actor, source_collection, target_day
  ) on conflict on constraint anki_review_day_markers_pkey
  do update set updated_at = now();

  for item in select value from jsonb_array_elements(reviews)
  loop
    item_review_id := item->>'id';
    item_card_id := item->>'card_id';
    item_time_text := item->>'time_ms';
    item_change_text := coalesce(item->>'changed_at', '0');
    if jsonb_typeof(item) is distinct from 'object'
      or jsonb_typeof(item->'id') is distinct from 'string'
      or jsonb_typeof(item->'card_id') is distinct from 'string'
      or jsonb_typeof(item->'time_ms') is distinct from 'number'
      or (item ? 'changed_at'
        and jsonb_typeof(item->'changed_at') is distinct from 'number')
      or item_review_id !~ '^[0-9]{10,16}$'
      or item_card_id !~ '^[0-9]{1,20}$'
      or item_time_text !~ '^[0-9]{1,7}$'
      or item_change_text !~ '^[0-9]{1,18}$' then
      raise exception 'invalid review event';
    end if;
    if item_card_id::numeric <= 0
      or item_time_text::numeric > 3600000
      or item_review_id::numeric < lower_ms
      or item_review_id::numeric >= upper_ms then
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
      time_ms = excluded.time_ms,
      change_version = excluded.change_version,
      deleted = false,
      deleted_at = null
    where excluded.change_version > current.change_version;
  end loop;

  for item in select value from jsonb_array_elements(removed_ids)
  loop
    item_review_id := item->>'id';
    item_card_id := item->>'card_id';
    item_change_text := coalesce(item->>'changed_at', '0');
    if jsonb_typeof(item) is distinct from 'object'
      or jsonb_typeof(item->'id') is distinct from 'string'
      or jsonb_typeof(item->'card_id') is distinct from 'string'
      or (item ? 'changed_at'
        and jsonb_typeof(item->'changed_at') is distinct from 'number')
      or item_review_id !~ '^[0-9]{10,16}$'
      or item_card_id !~ '^[0-9]{1,20}$'
      or item_change_text !~ '^[0-9]{1,18}$' then
      raise exception 'invalid removed review';
    end if;
    if item_card_id::numeric <= 0
      or item_review_id::numeric < lower_ms
      or item_review_id::numeric >= upper_ms then
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
      change_version = excluded.change_version,
      deleted = true,
      deleted_at = now()
    where excluded.change_version > current.change_version;
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
  ) then
    raise exception 'not a member of this group';
  end if;

  return query
  with device_rows as (
    select d.* from public.device_daily_stats d
    where d.group_id = target_group and d.study_day = target_day
  ), legacy_totals as (
    select d.user_id,
      sum(d.active_seconds)::bigint as active_seconds,
      sum(d.answer_count)::bigint as answer_count,
      max(d.updated_at) as updated_at
    from device_rows d group by d.user_id
  ), native_users as (
    select m.user_id, max(m.updated_at) as updated_at
    from public.anki_review_day_markers m
    where m.group_id = target_group and m.study_day = target_day
    group by m.user_id
  ), native_totals as (
    select n.user_id,
      (coalesce(sum(e.time_ms) filter (where not e.deleted), 0) / 1000)::bigint
        as active_seconds,
      (count(e.review_id) filter (where not e.deleted))::bigint as answer_count,
      greatest(n.updated_at, coalesce(max(e.created_at), n.updated_at)) as updated_at
    from native_users n
    left join public.anki_review_events e
      on e.group_id = target_group and e.user_id = n.user_id
      and e.study_day = target_day
    group by n.user_id, n.updated_at
  ), users_for_day as (
    select l.user_id from legacy_totals l
    union
    select n.user_id from native_totals n
  ), totals as (
    select u.user_id,
      case when n.user_id is not null then n.active_seconds
        else l.active_seconds end as active_seconds,
      case when n.user_id is not null then n.answer_count
        else l.answer_count end as answer_count,
      case when n.user_id is not null then
        greatest(n.updated_at, coalesce(l.updated_at, n.updated_at))
        else l.updated_at end as updated_at
    from users_for_day u
    left join legacy_totals l using (user_id)
    left join native_totals n using (user_id)
  ), latest as (
    select distinct on (d.user_id)
      d.user_id, d.time_goal_minutes, d.card_goal
    from device_rows d
    order by d.user_id, d.updated_at desc, d.device_id
  ), presence as (
    select t.user_id,
      case when exists (
        select 1 from device_rows d
        where d.user_id = t.user_id and d.status = 'studying'
          and d.updated_at >= now() - interval '90 seconds'
      ) then 'studying'
      else coalesce((
        select d.status from device_rows d
        where d.user_id = t.user_id and d.status in ('paused', 'stopped')
          and d.updated_at >= now() - interval '90 seconds'
        order by d.updated_at desc, d.device_id limit 1
      ), 'stopped') end as status
    from totals t
  )
  select target_group, t.user_id, target_day,
    t.active_seconds, t.answer_count,
    coalesce(l.time_goal_minutes, 0), coalesce(l.card_goal, 0),
    p.status, t.updated_at
  from totals t
  left join latest l using (user_id)
  join presence p using (user_id)
  order by t.updated_at desc, t.user_id;
end;
$$;

revoke all on function public.sync_review_day(uuid, text, date, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.sync_review_day(uuid, text, date, jsonb, jsonb)
  to authenticated;
revoke all on function public.get_group_device_stats(uuid, date)
  from public, anon, authenticated;
grant execute on function public.get_group_device_stats(uuid, date)
  to authenticated;

commit;
