import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import antigravity_probe as ag
import orbit_usage as orbit


def make_report(*groups):
    return {'status': 'SUCCESS', 'command': {'name': 'usage', 'data': {'groups': list(groups)}}}


class AntigravityTests(unittest.TestCase):
    def test_four_true_windows(self):
        data = make_report(
            {'displayName': 'Gemini Models', 'buckets': [
                {'id': 'gemini-5h', 'window': '5h', 'remaining_fraction': 0.82, 'reset_time': '2026-10-17T06:00:00Z'},
                {'id': 'gemini-weekly', 'window': 'weekly', 'remaining_fraction': 0.26, 'reset_time': '2026-10-18T11:00:00Z'}]},
            {'displayName': 'Claude and GPT models', 'buckets': [
                {'id': '3p-5h', 'window': '5h', 'remaining_fraction': 0.96},
                {'id': '3p-weekly', 'window': 'weekly', 'remaining_fraction': 0.53}]})
        results = ag.parse_agy_report(data)
        self.assertEqual(len(results['models']), 4)
        self.assertEqual([x['name'] for x in results['models']], [
            'Gemini Models · 5 hours', 'Gemini Models · 7 days',
            'Claude and GPT models · 5 hours', 'Claude and GPT models · 7 days'])
        with patch('antigravity_probe.get_usage', return_value=results):
            out = orbit.antigravity()
        self.assertEqual(out['models'][0]['used_percent'], 18.0)
        self.assertEqual(out['models'][1]['used_percent'], 74.0)
        self.assertEqual(out['models'][2]['used_percent'], 4.0)
        self.assertEqual(out['models'][3]['used_percent'], 47.0)
        self.assertIn('Gemini Models · 7 days', orbit.render({'checked_at': 'now', 'providers': {'antigravity': out}}))

    def test_compact_shows_every_model_family(self):
        """Compact AG output must include both Gemini and Claude/GPT pools,
        not only the single highest-usage window."""
        data = make_report(
            {'displayName': 'Gemini Models', 'buckets': [
                {'id': 'g5', 'window': '5h', 'remaining_fraction': 0.82},
                {'id': 'gw', 'window': 'weekly', 'remaining_fraction': 0.26}]},
            {'displayName': 'Claude and GPT models', 'buckets': [
                {'id': 'c5', 'window': '5h', 'remaining_fraction': 0.96},
                {'id': 'cw', 'window': 'weekly', 'remaining_fraction': 0.53}]})
        parsed = ag.parse_agy_report(data)
        with patch('antigravity_probe.get_usage', return_value=parsed):
            out = orbit.antigravity()
        compact = orbit.compact({'checked_at': 'now', 'providers': {'antigravity': out}})
        self.assertIn('Gemini', compact)
        self.assertIn('Claude/GPT', compact)
        self.assertIn('74%', compact)   # Gemini weekly 1-0.26
        self.assertIn('47%', compact)   # Claude/GPT weekly 1-0.53

    def test_missing_fraction_is_not_zero_or_full(self):
        g = {'name': 'Gemini Models', 'buckets': [
            {'id': 'gemini-5h', 'window': '5h', 'reset_time': '2026-10-18T00:00:00Z'},
            {'id': 'gemini-weekly', 'window': 'weekly', 'remaining_fraction': 1},
            {'id': 'gemini-disabled', 'window': '5h', 'remaining_fraction': 0, 'enabled': False}]}
        out = ag.parse_agy_report(make_report(g))
        self.assertEqual(len(out['models']), 1)
        self.assertEqual(out['models'][0]['remaining'], 1)

    def test_no_misleading_cadence(self):
        bad = {'name': 'Gemini Models', 'buckets': [{'id': 'gemini-rolling', 'remaining_fraction': .5}]}
        with self.assertRaisesRegex(RuntimeError, 'no enabled'):
            ag.parse_agy_report(make_report(bad))

    def test_not_chat_inference_output(self):
        with self.assertRaisesRegex(RuntimeError, 'structured'):
            ag.parse_agy_report({'status': 'SUCCESS', 'response': 'some normal model-generated text'})

    def test_weekly_only_starter(self):
        data = make_report({'displayName': 'Starter', 'buckets': [
            {'bucketId': 'gemini-weekly', 'remaining': {'remainingFraction': 0.95, 'resetTime': '2026-10-20T00:00:00Z'}}]})
        got = ag.parse_agy_report(data)['models']
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['remaining'], .95)
        self.assertEqual(got[0]['resetAt'], '2026-10-20T00:00:00Z')

    def test_invalid_out_of_bounds(self):
        x = {'name': 'Gemini', 'buckets': [
            {'window': '5h', 'remaining_fraction': -0.2},
            {'window': 'weekly', 'remaining_fraction': 1.4}]}
        with self.assertRaises(RuntimeError):
            ag.parse_agy_report(make_report(x))

    @unittest.skipIf(sys.platform == 'win32', 'POSIX fixture executable')
    def test_runs_real_mock_binary_only_usage_command(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / 'agy'
            report = make_report({'name': 'Gemini', 'buckets': [{'window': '5h', 'remaining_fraction': .9}]})
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                            f'assert sys.argv[1:] == ["-p", "/usage", "--output-format", "json"]\n'
                            f'print({json.dumps(json.dumps(report))})\n', encoding='utf-8')
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            with patch('antigravity_probe.find_agy', return_value=str(fake)), \
                    patch('antigravity_probe.agy_has_profile', return_value=True), \
                    patch('antigravity_probe.local_usage', side_effect=RuntimeError('must not use IDE')):
                got = ag.get_usage()
            self.assertEqual(got['source'], 'agy CLI /usage')
            self.assertEqual(got['models'][0]['remaining'], .9)

    def test_cli_failure_uses_ide_fallback(self):
        with patch('antigravity_probe.find_agy', return_value='/tmp/agybinary'), \
                patch('antigravity_probe._read_agy_report', side_effect=RuntimeError('bad login')), \
                patch('antigravity_probe.local_usage', return_value={'models': [{'name': 'Gemini', 'remaining': .3, 'resetAt': ''}], 'source': 'IDE', 'grouped': False}):
            result = ag.get_usage()
        self.assertEqual(result['source'], 'IDE')

    def test_friendly_failure_with_both_unavailable(self):
        with patch('antigravity_probe.find_agy', return_value=None), \
                patch('antigravity_probe.local_usage', side_effect=RuntimeError('no running IDE')):
            with self.assertRaisesRegex(RuntimeError, 'agy CLI not found.*no running IDE'):
                ag.get_usage()


if __name__ == '__main__':
    unittest.main()
