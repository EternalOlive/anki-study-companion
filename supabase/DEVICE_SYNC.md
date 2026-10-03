# Device sync activation

## Native Anki review totals (2026-10-04)

`migrations/20261004_synced_review_totals.sql` is now applied to production.
Its rollback regression passed on the production database before deployment;
all temporary users/rooms were rolled back. After commit, both tables, the
authenticated upload permission, anonymous denial, and private event access
were checked successfully.

- `sync_review_day` stores review identities and milliseconds, deduplicated by
  room/account/collection creation timestamp/review ID/card ID. No card content.
- Native day markers switch that account/day's totals from the legacy device
  sums to native events, including a genuinely empty day. Never add both.
- `record_device_day` remains the source of live PC status and personal goals.
- Client queries yesterday and today through Anki's serialized `QueryOp` queue
  after startup, collection operations, and Anki sync. Yesterday covers a final
  review not collected before closing at midnight; this is not full backfill.
- Batches and exact acknowledgements persist locally, isolated by account/room.
  New observations survive late callbacks. Same native events on two PCs do not
  double count, while disjoint reviews do contribute.
- Explicit observed PC undo/redo carries a monotonically increasing millisecond
  change version. Stale snapshots cannot revive a tombstone. Clock skew across
  PCs and remote/mobile undo reconciliation need further end-to-end testing.
- A missing row after ordinary sync/backup restore does not delete server data.
  Collection `crt` changes or separately copied/imported collections require
  caution: the namespace is Anki's synced creation timestamp, not a new UUID.
- Real mobile→AnkiWeb→PC end-to-end use has not yet been verified on a phone.

The following describes the legacy activity totals and current presence channel.

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
