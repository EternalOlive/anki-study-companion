-- Production cutover to idempotent per-device study totals.
-- Safe to paste as one transaction into the Supabase SQL editor.
-- Existing accounts, rooms, memberships and legacy daily_stats rows are kept.
begin;

create table if not exists public.device_daily_stats (
  group_id uuid not null,
  user_id uuid not null,
  device_id uuid not null,
  study_day date not null,
  revision bigint not null check (revision >= 0),
  active_seconds bigint not null check (active_seconds >= 0),
  answer_count bigint not null check (answer_count >= 0),
  time_goal_minutes integer not null default 0 check (time_goal_minutes between 0 and 1440),
  card_goal integer not null default 0 check (card_goal between 0 and 10000),
  status text not null check (status in ('studying', 'paused', 'stopped')),
  updated_at timestamptz not null default now(),
  primary key (group_id, user_id, device_id, study_day),
  foreign key (group_id, user_id)
    references public.group_members(group_id, user_id) on delete cascade
);

alter table public.device_daily_stats
  add column if not exists time_goal_minutes integer not null default 0
    check (time_goal_minutes between 0 and 1440),
  add column if not exists card_goal integer not null default 0
    check (card_goal between 0 and 10000);

alter table public.device_daily_stats enable row level security;
revoke all on public.device_daily_stats from public, anon, authenticated;

drop function if exists public.record_device_day(
  uuid, uuid, date, bigint, bigint, bigint, text
);

create or replace function public.record_device_day(
  target_group uuid,
  source_device uuid,
  target_day date,
  snapshot_revision bigint,
  seconds_total bigint,
  answers_total bigint,
  activity_status text,
  time_goal integer,
  cards_goal integer
)
returns setof public.device_daily_stats
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
begin
  if actor is null then raise exception 'authentication required'; end if;
  perform 1 from public.group_members
    where group_id = target_group and user_id = actor for key share;
  if not found then raise exception 'not a member of this group'; end if;
  if source_device is null or target_day is null
    or snapshot_revision is null or snapshot_revision < 0
    or seconds_total is null or seconds_total < 0
    or answers_total is null or answers_total < 0
    or activity_status is null
    or activity_status not in ('studying', 'paused', 'stopped')
    or time_goal is null or time_goal < 0 or time_goal > 1440
    or cards_goal is null or cards_goal < 0 or cards_goal > 10000 then
    raise exception 'invalid device snapshot';
  end if;

  insert into public.device_daily_stats as current (
    group_id, user_id, device_id, study_day, revision,
    active_seconds, answer_count, time_goal_minutes, card_goal, status
  ) values (
    target_group, actor, source_device, target_day, snapshot_revision,
    seconds_total, answers_total, time_goal, cards_goal, activity_status
  ) on conflict (group_id, user_id, device_id, study_day) do update set
    revision = excluded.revision,
    active_seconds = greatest(current.active_seconds, excluded.active_seconds),
    answer_count = greatest(current.answer_count, excluded.answer_count),
    time_goal_minutes = excluded.time_goal_minutes,
    card_goal = excluded.card_goal,
    status = excluded.status,
    updated_at = now()
  where excluded.revision > current.revision;

  return query select * from public.device_daily_stats
    where group_id = target_group and user_id = actor
      and device_id = source_device and study_day = target_day;
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
  with rows_for_day as (
    select d.* from public.device_daily_stats d
    where d.group_id = target_group and d.study_day = target_day
  ), totals as (
    select d.user_id,
      sum(d.active_seconds)::bigint as active_seconds,
      sum(d.answer_count)::bigint as answer_count,
      max(d.updated_at) as updated_at
    from rows_for_day d group by d.user_id
  ), latest as (
    select distinct on (d.user_id)
      d.user_id, d.time_goal_minutes, d.card_goal
    from rows_for_day d
    order by d.user_id, d.updated_at desc, d.device_id
  ), presence as (
    select t.user_id,
      case when exists (
        select 1 from rows_for_day d
        where d.user_id = t.user_id and d.status = 'studying'
          and d.updated_at >= now() - interval '90 seconds'
      ) then 'studying'
      else coalesce((
        select d.status from rows_for_day d
        where d.user_id = t.user_id and d.status in ('paused', 'stopped')
          and d.updated_at >= now() - interval '90 seconds'
        order by d.updated_at desc, d.device_id limit 1
      ), 'stopped') end as status
    from totals t
  )
  select target_group, t.user_id, target_day,
    t.active_seconds, t.answer_count, l.time_goal_minutes, l.card_goal,
    p.status, t.updated_at
  from totals t join latest l using (user_id) join presence p using (user_id)
  order by t.updated_at desc, t.user_id;
end;
$$;

revoke all on function public.record_device_day(
  uuid, uuid, date, bigint, bigint, bigint, text, integer, integer
) from public, anon, authenticated;
grant execute on function public.record_device_day(
  uuid, uuid, date, bigint, bigint, bigint, text, integer, integer
) to authenticated;
revoke all on function public.get_group_device_stats(uuid, date)
  from public, anon, authenticated;
grant execute on function public.get_group_device_stats(uuid, date)
  to authenticated;

drop policy if exists "users insert own daily stats" on public.daily_stats;
drop policy if exists "users update own daily stats" on public.daily_stats;
revoke insert, update on public.daily_stats from anon, authenticated;

commit;
