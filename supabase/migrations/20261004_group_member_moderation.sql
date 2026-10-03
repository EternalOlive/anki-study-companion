-- Let room owners remove members and prevent blocked accounts from rejoining.
-- Every membership insert is guarded so RPC and direct insert paths agree.
begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

create table if not exists public.study_group_bans (
  group_id uuid not null references public.study_groups(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  blocked_by uuid references auth.users(id) on delete set null,
  blocked_at timestamptz not null default now(),
  primary key (group_id, user_id)
);

alter table public.study_group_bans enable row level security;
revoke all on public.study_group_bans from public, anon, authenticated;

create or replace function public.guard_study_group_membership()
returns trigger
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
begin
  -- This lock shares the same group row used by moderation. If a join and a
  -- block overlap, one completes first and the final state remains blocked.
  perform 1
  from public.study_groups as groups
  where groups.id = new.group_id
  for key share;

  if exists (
    select 1
    from public.study_group_bans as bans
    where bans.group_id = new.group_id
      and bans.user_id = new.user_id
  ) then
    raise exception 'blocked from this group';
  end if;

  return new;
end;
$$;

revoke all on function public.guard_study_group_membership()
  from public, anon, authenticated;

drop trigger if exists guard_study_group_membership_insert
  on public.group_members;
create trigger guard_study_group_membership_insert
before insert on public.group_members
for each row execute function public.guard_study_group_membership();

create or replace function public.moderate_study_group_member(
  target_group uuid,
  target_user uuid,
  blocked boolean
)
returns void
language plpgsql
security definer
set search_path = ''
set lock_timeout = '5s'
as $$
declare
  actor uuid := auth.uid();
  owner uuid;
begin
  if actor is null then raise exception 'authentication required'; end if;
  if target_group is null or target_user is null or blocked is null then
    raise exception 'group, user and blocked state are required';
  end if;

  -- Serialize moderation with the membership guard before changing either
  -- the ban or membership row.
  select groups.owner_id into owner
  from public.study_groups as groups
  where groups.id = target_group
  for update;

  if owner is null or owner <> actor then
    raise exception 'only the group owner can moderate members';
  end if;
  if blocked and target_user = actor then
    raise exception 'the group owner cannot block themselves';
  end if;

  if blocked then
    insert into public.study_group_bans as bans (
      group_id, user_id, blocked_by, blocked_at
    ) values (
      target_group, target_user, actor, now()
    ) on conflict (group_id, user_id) do update set
      blocked_by = excluded.blocked_by,
      blocked_at = excluded.blocked_at;

    -- Match the existing leave flow for legacy totals. Tables keyed to the
    -- membership row are removed by their ON DELETE CASCADE constraints.
    delete from public.daily_stats
    where group_id = target_group and user_id = target_user;
    delete from public.group_members
    where group_id = target_group and user_id = target_user;
  else
    delete from public.study_group_bans
    where group_id = target_group and user_id = target_user;
  end if;
end;
$$;

create or replace function public.list_study_group_bans(target_group uuid)
returns table (user_id uuid)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  owner uuid;
begin
  if actor is null then raise exception 'authentication required'; end if;

  select groups.owner_id into owner
  from public.study_groups as groups
  where groups.id = target_group;

  if owner is null or owner <> actor then
    raise exception 'only the group owner can list blocked members';
  end if;

  return query
  select bans.user_id
  from public.study_group_bans as bans
  where bans.group_id = target_group
  order by bans.blocked_at desc, bans.user_id;
end;
$$;

revoke all on function public.moderate_study_group_member(uuid, uuid, boolean)
  from public, anon, authenticated;
grant execute on function public.moderate_study_group_member(uuid, uuid, boolean)
  to authenticated;
revoke all on function public.list_study_group_bans(uuid)
  from public, anon, authenticated;
grant execute on function public.list_study_group_bans(uuid)
  to authenticated;

commit;
