import json
import tempfile
import unittest
from pathlib import Path

from study_companion.online import (
    DeviceSyncLedger,
    SupabaseClient,
    load_or_create_device_id,
    profile_device_id,
)


class FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.raw

    def close(self):
        pass


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def __call__(self, request, timeout):
        self.request = request
        return FakeResponse(self.payload)


class DeviceIdentityTests(unittest.TestCase):
    def test_identity_is_stable_on_one_machine_and_rotates_after_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.json"
            first = load_or_create_device_id(path, "machine-a")
            self.assertEqual(load_or_create_device_id(path, "machine-a"), first)
            copied = load_or_create_device_id(path, "machine-b")
            self.assertNotEqual(copied, first)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["machine_marker"], "machine-b")

    def test_each_local_anki_profile_has_a_distinct_stable_stream(self):
        installation_id = "2f43acfc-31ef-4a38-81dd-4cf207c22526"
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "User 1"
            second_path = Path(directory) / "User 2"
            first = profile_device_id(installation_id, first_path)
            self.assertEqual(profile_device_id(installation_id, first_path), first)
            self.assertNotEqual(profile_device_id(installation_id, second_path), first)


class DeviceSyncLedgerTests(unittest.TestCase):
    def test_new_installation_drops_copied_device_counters(self):
        state = {
            "installation_id": "old-device",
            "active_user": "u1",
            "accounts": {"u1": {"2026-10-03": {"seconds_total": 999}}},
        }
        ledger = DeviceSyncLedger(state)
        ledger.bind_device("new-device")
        copied = ledger.prepare(
            user_id="u1", day="2026-10-03", active_seconds=120,
            answer_count=12, time_goal_minutes=0, card_goal=0,
            status="paused",
        )
        increment = ledger.prepare(
            user_id="u1", day="2026-10-03", active_seconds=140,
            answer_count=14, time_goal_minutes=0, card_goal=0,
            status="studying",
        )
        self.assertEqual(copied["active_seconds"], 0)
        self.assertEqual(copied["answer_count"], 0)
        self.assertEqual(increment["active_seconds"], 20)
        self.assertEqual(increment["answer_count"], 2)

    def test_snapshot_revision_is_stable_for_retry_and_increases_for_change(self):
        state = {}
        ledger = DeviceSyncLedger(state)
        args = dict(
            user_id="u1", day="2026-10-03", active_seconds=120,
            answer_count=8, time_goal_minutes=60, card_goal=100,
            status="paused",
        )
        first = ledger.prepare(**args)
        retry = ledger.prepare(**args)
        ledger.acknowledge("u1", "2026-10-03", first["revision"])
        heartbeat = ledger.prepare(**args)
        heartbeat_retry = ledger.prepare(**args)
        changed = ledger.prepare(**{**args, "active_seconds": 150})

        self.assertEqual(first["revision"], retry["revision"])
        self.assertEqual(retry["active_seconds"], 120)
        self.assertEqual(heartbeat["revision"], first["revision"] + 1)
        self.assertEqual(heartbeat_retry["revision"], heartbeat["revision"])
        self.assertEqual(changed["revision"], heartbeat["revision"] + 1)
        self.assertEqual(changed["active_seconds"], 150)

    def test_switching_accounts_does_not_share_local_study(self):
        ledger = DeviceSyncLedger({})
        first = ledger.prepare(
            user_id="u1", day="2026-10-03", active_seconds=100,
            answer_count=10, time_goal_minutes=0, card_goal=0,
            status="paused",
        )
        ledger.activate("u2", "2026-10-03", 100, 10)
        second = ledger.prepare(
            user_id="u2", day="2026-10-03", active_seconds=140,
            answer_count=14, time_goal_minutes=0, card_goal=0,
            status="paused",
        )
        ledger.activate("u1", "2026-10-03", 140, 14)
        resumed = ledger.prepare(
            user_id="u1", day="2026-10-03", active_seconds=160,
            answer_count=16, time_goal_minutes=0, card_goal=0,
            status="paused",
        )

        self.assertEqual(first["active_seconds"], 100)
        self.assertEqual(second["active_seconds"], 40)
        self.assertEqual(resumed["active_seconds"], 120)
        self.assertEqual(resumed["answer_count"], 12)

    def test_new_day_for_same_account_starts_from_that_days_zero(self):
        ledger = DeviceSyncLedger({})
        ledger.prepare(
            user_id="u1", day="2026-10-03", active_seconds=300,
            answer_count=20, time_goal_minutes=0, card_goal=0,
            status="stopped",
        )
        next_day = ledger.prepare(
            user_id="u1", day="2026-10-04", active_seconds=12,
            answer_count=1, time_goal_minutes=0, card_goal=0,
            status="studying",
        )
        self.assertEqual(next_day["active_seconds"], 12)
        self.assertEqual(next_day["answer_count"], 1)


class DeviceSyncRequestTests(unittest.TestCase):
    def test_device_request_includes_goals(self):
        opener = FakeOpener([{"revision": 3}])
        client = SupabaseClient(opener=opener)
        client.record_device_day(
            "token", group_id="g1", device_id="d1", study_day="2026-10-03",
            revision=3, active_seconds=100, answer_count=9, status="studying",
            time_goal_minutes=90, card_goal=120,
        )
        body = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(body["time_goal"], 90)
        self.assertEqual(body["cards_goal"], 120)
        self.assertNotIn("user_id", body)


if __name__ == "__main__":
    unittest.main()
