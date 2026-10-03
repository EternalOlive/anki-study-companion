-- Run after 20261003_username_accounts.sql on a test database.
-- All fixtures and mutations are rolled back. Any failed assertion aborts.
begin;
do $$
declare
  first_user uuid := gen_random_uuid();
  second_user uuid := gen_random_uuid();
  first_lease uuid := gen_random_uuid();
  second_lease uuid := gen_random_uuid();
  repair_lease uuid := gen_random_uuid();
  recovery_lease uuid := gen_random_uuid();
  first_name text := 'test_' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 12);
  pending_name text := 'test_' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 12);
  old_digest text := encode(digest('old recovery code', 'sha256'), 'hex');
  new_digest text := encode(digest('new recovery code', 'sha256'), 'hex');
  result jsonb;
begin
  insert into auth.users(id) values (first_user), (second_user);

  result := public.account_auth_begin_bind(
    first_name, first_user, true, first_lease);
  assert result->>'state' = 'pending';
  assert (result->>'user_id')::uuid = first_user;

  begin
    perform public.account_auth_begin_bind(
      first_name, second_user, true, gen_random_uuid());
    raise exception 'duplicate username accepted';
  exception when raise_exception then
    if sqlerrm <> 'USERNAME_UNAVAILABLE' then raise; end if;
  end;

  assert public.account_auth_finish_bind(
    first_name, first_user, first_lease, old_digest);
  assert exists (
    select 1 from public.account_credentials
    where username = first_name and user_id = first_user and state = 'active'
      and recovery_hash = decode(old_digest, 'hex') and lease_token is null
  );

  -- Repeating bind for an active account reserves a lease. The Edge Function
  -- must authenticate the supplied current password before rotating recovery.
  result := public.account_auth_begin_bind(
    first_name, first_user, false, second_lease);
  assert result->>'state' = 'active';
  perform public.account_auth_release_lease(first_name, first_user, second_lease);

  begin
    perform public.account_auth_begin_recovery(
      first_name, new_digest, gen_random_uuid());
    raise exception 'incorrect recovery accepted';
  exception when raise_exception then
    if sqlerrm <> 'INVALID_RECOVERY' then raise; end if;
  end;

  result := public.account_auth_begin_recovery(
    first_name, old_digest, recovery_lease);
  assert (result->>'user_id')::uuid = first_user;
  assert public.account_auth_finish_recovery(
    first_name, first_user, recovery_lease, new_digest);
  assert exists (
    select 1 from public.account_credentials
    where username = first_name and recovery_hash = decode(new_digest, 'hex')
  );

  -- A bind that updated Auth but failed before database finalization can be
  -- repaired by a password login after its short lease is released/expires.
  perform public.account_auth_begin_bind(
    pending_name, second_user, true, repair_lease);
  perform public.account_auth_release_lease(pending_name, second_user, repair_lease);
  repair_lease := gen_random_uuid();
  result := public.account_auth_begin_login(pending_name, repair_lease);
  assert result->>'state' = 'pending';
  assert public.account_auth_finish_bind(
    pending_name, second_user, repair_lease, old_digest);

  result := public.account_auth_rate_limit(
    'username', repeat('a', 64), 'login', 2, 600);
  assert (result->>'allowed')::boolean;
  result := public.account_auth_rate_limit(
    'username', repeat('a', 64), 'login', 2, 600);
  assert (result->>'allowed')::boolean;
  result := public.account_auth_rate_limit(
    'username', repeat('a', 64), 'login', 2, 600);
  assert not (result->>'allowed')::boolean;
  assert (result->>'retry_after')::integer > 0;

  assert not has_table_privilege('anon', 'public.account_credentials', 'SELECT');
  assert not has_table_privilege('authenticated', 'public.account_credentials', 'SELECT');
  assert not has_table_privilege('anon', 'public.account_auth_rate_limits', 'SELECT');
  assert not has_function_privilege('anon',
    'public.account_auth_begin_login(text,uuid)', 'EXECUTE');
  assert not has_function_privilege('authenticated',
    'public.account_auth_begin_recovery(text,text,uuid)', 'EXECUTE');
  assert has_function_privilege('service_role',
    'public.account_auth_begin_login(text,uuid)', 'EXECUTE');
end;
$$;
rollback;
