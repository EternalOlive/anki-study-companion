create extension if not exists pgcrypto;

create or replace function public.generate_invite_code()
returns text
language sql
volatile
set search_path = ''
as $$
  select string_agg(
    substr('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', 1 + floor(random() * 32)::integer, 1),
    ''
  )
  from generate_series(1, 4);
$$;

revoke all on function public.generate_invite_code() from public;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null check (char_length(display_name) between 1 and 30),
  updated_at timestamptz not null default now()
);

create table if not exists public.study_groups (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 1 and 50),
  invite_code text not null unique default public.generate_invite_code(),
  owner_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

create table if not exists public.group_members (
  group_id uuid not null references public.study_groups(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  joined_at timestamptz not null default now(),
  primary key (group_id, user_id)
);

create table if not exists public.daily_stats (
  group_id uuid not null references public.study_groups(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  study_day date not null,
  active_seconds integer not null default 0 check (active_seconds >= 0),
  answer_count integer not null default 0 check (answer_count >= 0),
  time_goal_minutes integer not null default 0 check (time_goal_minutes between 0 and 1440),
  card_goal integer not null default 0 check (card_goal between 0 and 10000),
  status text not null default 'stopped' check (status in ('studying', 'paused', 'stopped')),
  updated_at timestamptz not null default now(),
  primary key (group_id, user_id, study_day)
);

alter table public.profiles enable row level security;
alter table public.study_groups enable row level security;
alter table public.group_members enable row level security;
alter table public.daily_stats enable row level security;

create or replace function public.is_group_member(target_group uuid)
returns boolean
language sql
security definer
set search_path = public
stable
as $$
  select exists (
    select 1 from public.group_members
    where group_id = target_group and user_id = auth.uid()
  );
$$;

revoke all on function public.is_group_member(uuid) from public;

create or replace function public.create_study_group(group_name text)
returns table (group_id uuid, invite_code text)
language plpgsql
security definer
set search_path = public
as $$
declare
  created_group public.study_groups;
  attempt integer;
begin
  if auth.uid() is null then raise exception 'authentication required'; end if;
  for attempt in 1..10 loop
    begin
      insert into public.study_groups(name, invite_code, owner_id)
      values (trim(group_name), public.generate_invite_code(), auth.uid())
      returning * into created_group;
      exit;
    exception when unique_violation then
      if attempt = 10 then
        raise exception 'could not allocate invite code';
      end if;
    end;
  end loop;
  insert into public.group_members(group_id, user_id)
  values (created_group.id, auth.uid());
  return query select created_group.id, created_group.invite_code;
end;
$$;

revoke all on function public.create_study_group(text) from public;

create or replace function public.join_study_group(code text)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  target_group uuid;
begin
  if auth.uid() is null then raise exception 'authentication required'; end if;
  select id into target_group from public.study_groups
  where invite_code = upper(trim(code));
  if target_group is null then raise exception 'invalid invite code'; end if;
  insert into public.group_members(group_id, user_id)
  values (target_group, auth.uid()) on conflict do nothing;
  return target_group;
end;
$$;

revoke all on function public.join_study_group(text) from public;

create or replace function public.leave_study_group(target_group uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  current_user_id uuid := auth.uid();
  next_owner_id uuid;
begin
  if current_user_id is null then raise exception 'authentication required'; end if;
  if not exists (
    select 1 from public.group_members
    where group_id = target_group and user_id = current_user_id
  ) then
    raise exception 'not a member of this group';
  end if;

  delete from public.daily_stats
  where group_id = target_group and user_id = current_user_id;
  delete from public.group_members
  where group_id = target_group and user_id = current_user_id;

  if exists (
    select 1 from public.study_groups
    where id = target_group and owner_id = current_user_id
  ) then
    select user_id into next_owner_id
    from public.group_members
    where group_id = target_group
    order by joined_at asc, user_id asc
    limit 1;

    if next_owner_id is null then
      delete from public.study_groups where id = target_group;
    else
      update public.study_groups
      set owner_id = next_owner_id
      where id = target_group;
    end if;
  end if;
end;
$$;

revoke all on function public.leave_study_group(uuid) from public;

create policy "profiles visible to group peers" on public.profiles
for select to authenticated
using (
  id = auth.uid() or exists (
    select 1 from public.group_members mine
    join public.group_members peer on peer.group_id = mine.group_id
    where mine.user_id = auth.uid() and peer.user_id = profiles.id
  )
);

create policy "users insert own profile" on public.profiles
for insert to authenticated with check (id = auth.uid());

create policy "users update own profile" on public.profiles
for update to authenticated using (id = auth.uid()) with check (id = auth.uid());

create policy "members view groups" on public.study_groups
for select to authenticated using (public.is_group_member(id));

create policy "members view memberships" on public.group_members
for select to authenticated using (public.is_group_member(group_id));

create policy "members view daily stats" on public.daily_stats
for select to authenticated using (public.is_group_member(group_id));

create policy "users insert own daily stats" on public.daily_stats
for insert to authenticated
with check (user_id = auth.uid() and public.is_group_member(group_id));

create policy "users update own daily stats" on public.daily_stats
for update to authenticated
using (user_id = auth.uid() and public.is_group_member(group_id))
with check (user_id = auth.uid() and public.is_group_member(group_id));

grant execute on function public.create_study_group(text) to authenticated;
grant execute on function public.join_study_group(text) to authenticated;
grant execute on function public.leave_study_group(uuid) to authenticated;
grant execute on function public.is_group_member(uuid) to authenticated;
