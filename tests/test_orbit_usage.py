import datetime as dt
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orbit_usage as o
from kiro_usage import extract_quota


class ParsingTests(unittest.TestCase):
    def test_codex_primary_secondary(self):
        data = {'rate_limit': {'primary_window': {'used_percent': 31, 'limit_window_seconds': 18000, 'resets_at': 1790000000},
                               'secondary_window': {'used_percent': 77, 'limit_window_seconds': 604800}}}
        parsed = o.codex_windows(data)
        self.assertEqual(parsed['five_hour']['used_percent'], 31)
        self.assertEqual(parsed['weekly']['used_percent'], 77)

    def test_codex_captures_offset_reset(self):
        data = {'rate_limits': {'primary': {'used_percent': 25, 'resets_in_seconds': 600}}}
        parsed = o.codex_windows(data, '2026-10-16T20:00:00Z')
        self.assertEqual(parsed['five_hour']['resets_at'], '2026-10-16T20:10:00Z')

    def test_codex_percent_left(self):
        self.assertEqual(o.codex_windows({'primary': {'percent_left': 22}})['five_hour']['used_percent'], 78)

    def test_kiro_quota_prefers_credits(self):
        payload = {'usageBreakdowns': [
            {'displayName': 'Bonus', 'used': 3, 'limit': 5},
            {'displayName': 'Subscription Credits', 'used': 19, 'limit': 100},
        ], 'billingCycleReset': '2026-11-01T00:00:00Z'}
        q = extract_quota(payload)
        self.assertEqual((q['used'], q['limit']), (19, 100))

    def test_finite_percent(self):
        self.assertEqual(o.clamp_percent(113), 100)
        self.assertIsNone(o.clamp_percent('not-a-number'))

    def test_json_snapshot_missing_credentials_never_zero(self):
        with patch.dict(o.COLLECTORS, {'claude': lambda **_: (_ for _ in ()).throw(RuntimeError('no credentials'))}):
            data = o.snapshot(['claude'])
        self.assertEqual(data['providers']['claude']['status'], 'unavailable')
        self.assertNotIn('five_hour', data['providers']['claude'])

    def test_compact_distinguishes_quota_and_cursor(self):
        data = {'providers': {'claude': {'status': 'ok', 'five_hour': {'used_percent': 14}, 'weekly': {'used_percent': 85}},
                              'cursor': {'status': 'ok', 'authenticated': True}}}
        line = o.compact(data)
        self.assertIn('5h 14%', line)
        self.assertIn('7d 85%', line)
        self.assertIn('quota n/a', line)

    def test_datetime_epoch(self):
        self.assertEqual(o.parse_dt(0).year, 1970)


if __name__ == '__main__':
    unittest.main()
