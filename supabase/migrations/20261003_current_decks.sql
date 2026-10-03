-- Share only the deck currently being studied by each active device.
-- Existing study totals and presence RPCs are intentionally unchanged.
begin;

create table if not exists public.device_current_decks (
  group_id uuid not null,
  user_id uuid not null,
  device_id uuid not null,
  current_deck_name text not null check (char_length(current_deck_name) <= 300),
  updated_at timestamptz not null default now(),
  primary key (group_id, user_id, device_id),
  foreign key (group_id, user_id)
    references public.group_members(group_id, user_id) on delete cascade
);

create index if not exists device_current_decks_group_updated_idx
  on public.device_current_decks(group_id, updated_at desc);

alter table public.device_current_decks enable row level security;
revoke all on public.device_current_decks from public, anon, authenticated;

create or replace function public.set_current_deck(
  target_group uuid,
  source_device uuid,
  deck_name text
)
returns void
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
  if source_device is null then raise exception 'source device required'; end if;
  if deck_name is not null and char_length(deck_name) > 300 then
    raise exception 'deck name too long';
  end if;

  if deck_name is null then
    delete from public.device_current_decks
    where group_id = target_group
      and user_id = actor
      and device_id = source_device;
    return;
  end if;

  insert into public.device_current_decks as current (
    group_id, user_id, device_id, current_deck_name, updated_at
  ) values (
    target_group, actor, source_device, deck_name, now()
  ) on conflict (group_id, user_id, device_id) do update set
    current_deck_name = excluded.current_deck_name,
    updated_at = now();
end;
$$;

create or replace function public.get_group_current_decks(
  target_group uuid
)
returns table (
  user_id uuid,
  current_deck_name text,
  deck_updated_at timestamptz
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
  if not exists (
    select 1 from public.group_members gm
    where gm.group_id = target_group and gm.user_id = actor
  ) then
    raise exception 'not a member of this group';
  end if;

  return query
  select distinct on (d.user_id)
    d.user_id,
    d.current_deck_name,
    d.updated_at as deck_updated_at
  from public.device_current_decks d
  where d.group_id = target_group
    and d.updated_at >= now() - interval '90 seconds'
  order by d.user_id, d.updated_at desc, d.device_id;
end;
$$;

revoke all on function public.set_current_deck(uuid, uuid, text)
  from public, anon, authenticated;
grant execute on function public.set_current_deck(uuid, uuid, text)
  to authenticated;
revoke all on function public.get_group_current_decks(uuid)
  from public, anon, authenticated;
grant execute on function public.get_group_current_decks(uuid)
  to authenticated;

commit;
