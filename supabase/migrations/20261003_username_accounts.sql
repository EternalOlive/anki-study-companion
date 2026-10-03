-- Username/password accounts layered on top of Supabase anonymous users.
-- The Edge Function is the only caller: tables and helper RPCs are service-role only.
begin;

create table if not exists public.account_credentials (
  username text collate "C" primary key
    check (username ~ '^[a-z0-9_]{4,24}$'),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  state text not null default 'pending'
    check (state in ('pending', 'active')),
  recovery_hash bytea,
  lease_token uuid,
  lease_purpose text check (lease_purpose in ('bind', 'login', 'recover')),
  lease_expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (recovery_hash is null or octet_length(recovery_hash) = 32),
  check (
    (state = 'pending' and recovery_hash is null)
    or (state = 'active' and recovery_hash is not null)
  ),
  check (
    (lease_token is null and lease_purpose is null and lease_expires_at is null)
    or
    (lease_token is not null and lease_purpose is not null and lease_expires_at is not null)
  )
);

create table if not exists public.account_auth_rate_limits (
  scope text not null check (scope in ('ip', 'username')),
  subject_hash text not null check (subject_hash ~ '^[0-9a-f]{64}$'),
  operation text not null check (operation in ('all', 'bind', 'login', 'recover')),
  window_started_at timestamptz not null,
  attempt_count integer not null check (attempt_count > 0),
  updated_at timestamptz not null default now(),
  primary key (scope, subject_hash, operation)
);

alter table public.account_credentials enable row level security;
alter table public.account_auth_rate_limits enable row level security;
revoke all on public.account_credentials from public, anon, authenticated;
revoke all on public.account_auth_rate_limits from public, anon, authenticated;

create or replace function public.account_auth_rate_limit(
  target_scope text,
  target_subject_hash text,
  target_operation text,
  maximum_attempts integer,
  window_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  current_row public.account_auth_rate_limits;
  now_at timestamptz := clock_timestamp();
  allowed boolean;
  retry_after integer := 0;
begin
  if target_scope not in ('ip', 'username')
    or target_subject_hash !~ '^[0-9a-f]{64}$'
    or target_operation not in ('all', 'bind', 'login', 'recover')
    or maximum_attempts < 1 or maximum_attempts > 100
    or window_seconds < 10 or window_seconds > 86400 then
    raise exception 'INVALID_ARGUMENT';
  end if;

  insert into public.account_auth_rate_limits as limits (
    scope, subject_hash, operation, window_started_at, attempt_count, updated_at
  ) values (
    target_scope, target_subject_hash, target_operation, now_at, 1, now_at
  )
  on conflict (scope, subject_hash, operation) do update set
    window_started_at = case
      when limits.window_started_at + make_interval(secs => window_seconds) <= now_at
        then now_at else limits.window_started_at end,
    attempt_count = case
      when limits.window_started_at + make_interval(secs => window_seconds) <= now_at
        then 1 else limits.attempt_count + 1 end,
    updated_at = now_at
  returning * into current_row;

  allowed := current_row.attempt_count <= maximum_attempts;
  if not allowed then
    retry_after := greatest(1, ceil(extract(epoch from (
      current_row.window_started_at + make_interval(secs => window_seconds) - now_at
    )))::integer);
  end if;
  return jsonb_build_object('allowed', allowed, 'retry_after', retry_after);
end;
$$;

create or replace function public.account_auth_begin_bind(
  target_username text,
  target_user uuid,
  caller_is_anonymous boolean,
  claim_token uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  account public.account_credentials;
  now_at timestamptz := clock_timestamp();
begin
  if target_username !~ '^[a-z0-9_]{4,24}$'
    or target_user is null or caller_is_anonymous is null or claim_token is null then
    raise exception 'INVALID_ARGUMENT';
  end if;

  select * into account from public.account_credentials
    where username = target_username for update;
  if not found then
    if not caller_is_anonymous then raise exception 'BIND_REQUIRES_GUEST'; end if;
    if exists (
      select 1 from public.account_credentials where user_id = target_user
    ) then
      raise exception 'USER_ALREADY_LINKED';
    end if;
    begin
      insert into public.account_credentials (
        username, user_id, state, lease_token, lease_purpose, lease_expires_at
      ) values (
        target_username, target_user, 'pending', claim_token, 'bind',
      now_at + interval '2 minutes'
      ) returning * into account;
    exception when unique_violation then
      raise exception 'USERNAME_UNAVAILABLE';
    end;
  else
    if account.user_id <> target_user then raise exception 'USERNAME_UNAVAILABLE'; end if;
    if account.lease_token is not null and account.lease_expires_at > now_at then
      raise exception 'ACCOUNT_BUSY';
    end if;
    update public.account_credentials set
      lease_token = claim_token,
      lease_purpose = 'bind',
      lease_expires_at = now_at + interval '2 minutes',
      updated_at = now_at
    where username = target_username
    returning * into account;
  end if;

  return jsonb_build_object('state', account.state, 'user_id', account.user_id);
end;
$$;

create or replace function public.account_auth_begin_login(
  target_username text,
  claim_token uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  account public.account_credentials;
  now_at timestamptz := clock_timestamp();
begin
  if target_username !~ '^[a-z0-9_]{4,24}$' or claim_token is null then
    raise exception 'INVALID_ARGUMENT';
  end if;
  select * into account from public.account_credentials
    where username = target_username for update;
  if not found then raise exception 'INVALID_LOGIN'; end if;

  if account.state = 'pending' then
    if account.lease_token is not null and account.lease_expires_at > now_at then
      raise exception 'ACCOUNT_BUSY';
    end if;
    update public.account_credentials set
      lease_token = claim_token,
      lease_purpose = 'login',
      lease_expires_at = now_at + interval '2 minutes',
      updated_at = now_at
    where username = target_username
    returning * into account;
  end if;
  return jsonb_build_object('state', account.state, 'user_id', account.user_id);
end;
$$;

create or replace function public.account_auth_finish_bind(
  target_username text,
  target_user uuid,
  claim_token uuid,
  recovery_digest text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  if recovery_digest !~ '^[0-9a-f]{64}$' then raise exception 'INVALID_ARGUMENT'; end if;
  update public.account_credentials set
    state = 'active',
    recovery_hash = decode(recovery_digest, 'hex'),
    lease_token = null,
    lease_purpose = null,
    lease_expires_at = null,
    updated_at = clock_timestamp()
  where username = target_username and user_id = target_user
    and lease_token = claim_token and lease_purpose in ('bind', 'login')
    and lease_expires_at > clock_timestamp();
  if not found then raise exception 'LEASE_LOST'; end if;
  return true;
end;
$$;

create or replace function public.account_auth_begin_recovery(
  target_username text,
  recovery_digest text,
  claim_token uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  account public.account_credentials;
  now_at timestamptz := clock_timestamp();
begin
  if target_username !~ '^[a-z0-9_]{4,24}$'
    or recovery_digest !~ '^[0-9a-f]{64}$' or claim_token is null then
    raise exception 'INVALID_ARGUMENT';
  end if;
  select * into account from public.account_credentials
    where username = target_username for update;
  if not found or account.state <> 'active'
    or account.recovery_hash is null
    or account.recovery_hash <> decode(recovery_digest, 'hex') then
    raise exception 'INVALID_RECOVERY';
  end if;
  if account.lease_token is not null and account.lease_expires_at > now_at then
    raise exception 'ACCOUNT_BUSY';
  end if;
  update public.account_credentials set
    lease_token = claim_token,
    lease_purpose = 'recover',
    lease_expires_at = now_at + interval '2 minutes',
    updated_at = now_at
  where username = target_username
  returning * into account;
  return jsonb_build_object('state', account.state, 'user_id', account.user_id);
end;
$$;

create or replace function public.account_auth_finish_recovery(
  target_username text,
  target_user uuid,
  claim_token uuid,
  recovery_digest text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  if recovery_digest !~ '^[0-9a-f]{64}$' then raise exception 'INVALID_ARGUMENT'; end if;
  update public.account_credentials set
    recovery_hash = decode(recovery_digest, 'hex'),
    lease_token = null,
    lease_purpose = null,
    lease_expires_at = null,
    updated_at = clock_timestamp()
  where username = target_username and user_id = target_user
    and state = 'active' and lease_token = claim_token
    and lease_purpose = 'recover' and lease_expires_at > clock_timestamp();
  if not found then raise exception 'LEASE_LOST'; end if;
  return true;
end;
$$;

create or replace function public.account_auth_release_lease(
  target_username text,
  target_user uuid,
  claim_token uuid
)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.account_credentials set
    lease_token = null, lease_purpose = null, lease_expires_at = null,
    updated_at = clock_timestamp()
  where username = target_username and user_id = target_user
    and lease_token = claim_token;
$$;

revoke all on function public.account_auth_rate_limit(text,text,text,integer,integer)
  from public, anon, authenticated;
revoke all on function public.account_auth_begin_bind(text,uuid,boolean,uuid)
  from public, anon, authenticated;
revoke all on function public.account_auth_begin_login(text,uuid)
  from public, anon, authenticated;
revoke all on function public.account_auth_finish_bind(text,uuid,uuid,text)
  from public, anon, authenticated;
revoke all on function public.account_auth_begin_recovery(text,text,uuid)
  from public, anon, authenticated;
revoke all on function public.account_auth_finish_recovery(text,uuid,uuid,text)
  from public, anon, authenticated;
revoke all on function public.account_auth_release_lease(text,uuid,uuid)
  from public, anon, authenticated;

grant execute on function public.account_auth_rate_limit(text,text,text,integer,integer)
  to service_role;
grant execute on function public.account_auth_begin_bind(text,uuid,boolean,uuid)
  to service_role;
grant execute on function public.account_auth_begin_login(text,uuid)
  to service_role;
grant execute on function public.account_auth_finish_bind(text,uuid,uuid,text)
  to service_role;
grant execute on function public.account_auth_begin_recovery(text,text,uuid)
  to service_role;
grant execute on function public.account_auth_finish_recovery(text,uuid,uuid,text)
  to service_role;
grant execute on function public.account_auth_release_lease(text,uuid,uuid)
  to service_role;

commit;
