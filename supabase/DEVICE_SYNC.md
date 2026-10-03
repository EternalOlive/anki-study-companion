# Device sync activation

Apply `migrations/20261003_activate_device_sync.sql` as one transaction. It
keeps accounts, rooms, memberships and old `daily_stats` rows, but new reads
intentionally ignore those legacy totals. This is the accepted reset boundary.

## Active storage contract

- Existing databases need only `20261003_activate_device_sync.sql`. On a new
  test database, apply `schema.sql` and then the activation migration.
- `record_device_day` accepts cumulative per-device counters and a persisted,
  increasing revision per group/account/device/day. Retry the identical snapshot
  with the same revision. A newer revision is needed for a presence heartbeat.
- Authentication determines the user; clients cannot supply another user's ID.
- Older revisions and duplicate requests do not replace counters or presence.
  Newer revisions cannot reduce counters. Receipt time is server-owned.
- Device rows are private to the RPC, not directly accessible by peers.
- Membership deletion cascades to device rows, including when leaving a room.
- `get_group_device_stats` sums every device for a member/day and returns the
  legacy daily-stats field shape. Goals come from the latest received device.
- A fresh studying heartbeat (90 seconds) takes precedence across devices.
  Otherwise the latest fresh paused/stopped state is used; stale users stop.
- Old clients lose INSERT/UPDATE access to `daily_stats`, preventing continued
  aggregate overwrites. Old rows are retained for recovery but not read.

## Verification

1. Run `tests/device_daily_stats.sql` on a disposable Supabase/Postgres database.
   It creates temporary test users inside a rolled-back transaction. Also test
   concurrent requests and concurrent room departure using two connections.
2. The client persists a durable installation identity and device ledger before
   requests, and only acknowledges a snapshot after the server returns its row.
3. Verify retries, two PCs, midnight, restored profiles, account switching and
   room rejoining against the deployed project.
4. Verify actual email linking and login on two PCs before calling email recovery
   production-tested.

2026-10-03: the activation transaction was applied to the production project.
The rollback regression was executed successfully against that database, covering
two device totals, duplicate/older revisions, goals, presence expiry, membership
checks, table privileges and leave cascade. No test fixtures were retained.
True simultaneous two-connection races and actual email delivery remain unverified.
