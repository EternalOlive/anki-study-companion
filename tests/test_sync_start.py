"""Test the real sync entry point without Qt or network access."""
import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from study_companion.online import DeviceSyncLedger
from study_companion.outbox import SyncOutbox


class SyncStartTests(TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / 'study_companion' / 'addon.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Controller')
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'sync_async']
        self.schedule = Mock()
        self.clock = Mock(return_value=datetime(2026, 10, 3, 23, 59, 59, tzinfo=timezone.utc))
        scope = {'now': self.clock, 'canonical_nickname': lambda uid: 'ABC-DEF',
                 'mw': SimpleNamespace(taskman=SimpleNamespace(run_in_background=self.schedule))}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), scope)
        self.controller = scope['Controller']()
        self.controller.closed = False
        self.controller.sync_in_flight = False
        self.controller.sync_pending = False
        self.controller.identity_generation = 0
        self.controller._access_token = lambda: 'token'
        self.controller.online = {'auth': {'user_id': 'u', 'access_token': 'token'},
                                  'group': {'id': 'room'}}
        self.controller.tracker = SimpleNamespace(
            today=lambda current: {'seconds': 10, 'answers': 2},
            time_goal_minutes=60, card_goal=100, status='studying')
        self.controller.device_id = 'device'
        self.controller.device_ledger = DeviceSyncLedger({})
        self.controller.sync_outbox = SyncOutbox({})
        self.controller.save = Mock()
        self.controller.t = lambda ko, en: ko

    def test_failed_local_save_does_not_upload_and_can_retry(self):
        self.controller.save.side_effect = OSError('disk full')
        self.controller.sync_async()
        self.assertFalse(self.controller.sync_in_flight)
        self.schedule.assert_not_called()
        self.assertIn('last_error', self.controller.online)
        self.controller.save.side_effect = None
        self.controller.sync_async()
        self.schedule.assert_called_once()
        self.assertTrue(self.controller.sync_in_flight)

    def test_scheduler_failure_releases_in_flight_guard(self):
        self.schedule.side_effect = RuntimeError('executor unavailable')
        self.controller.sync_async()
        self.assertFalse(self.controller.sync_in_flight)
        self.schedule.side_effect = None
        self.controller.sync_async()
        self.assertEqual(self.schedule.call_count, 2)

    def test_one_timestamp_is_used_for_day_and_record(self):
        self.controller.sync_async()
        self.clock.assert_called_once()
