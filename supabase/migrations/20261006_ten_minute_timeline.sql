-- Update get_group_activity_timeline to bin activity into 10-minute slots (144 slots per day).
begin;

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
          at time zone room.time_zone))::integer * 6
        + floor(extract(minute from (to_timestamp(e.review_id::numeric / 1000.0)
          at time zone room.time_zone)) / 10)::integer
        - room.day_start_hour * 6 + 144,
        144
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

revoke all on function public.get_group_activity_timeline(uuid, date)
  from public, anon, authenticated;
grant execute on function public.get_group_activity_timeline(uuid, date)
  to authenticated;

notify pgrst, 'reload schema';

commit;
