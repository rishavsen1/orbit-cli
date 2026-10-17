import base64
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cursor_probe as c
import orbit_usage as o


def jwt(sub='user_ABC'):
    payload = base64.urlsafe_b64encode(json.dumps({'sub': sub, 'type': 'session', 'aud': 'https://cursor.com'}).encode()).decode().rstrip('=')
    return 'eyJhbGciOiJub25lIn0.' + payload + '.signature'


class CursorCollectorTests(unittest.TestCase):
    def test_jwt_to_cookie_and_cookie_override(self):
        raw = jwt()
        self.assertEqual(c._session_token(raw), 'user_ABC::' + raw)
        self.assertEqual(c._session_token('user_ABC%3A%3A'+raw), 'user_ABC::'+raw)
        self.assertEqual(c._session_token('WorkosCursorSessionToken=user_ABC%3A%3A' + raw), 'user_ABC::'+raw)

    def test_bad_credentials_never_accepted(self):
        for token in ('crsr_abcdef', 'asdf', 'bogus::notjwt', 'x.y.z'):
            self.assertIsNone(c._session_token(token))

    def test_local_db_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'state.vscdb'
            with sqlite3.connect(p) as db:
                db.execute('CREATE TABLE ItemTable(key TEXT PRIMARY KEY, value TEXT)')
                db.execute('INSERT INTO ItemTable VALUES(?,?)', ('cursorAuth/accessToken', jwt()))
            self.assertEqual(c._database_token(p), jwt())
            self.assertIsNone(c._database_token(p.parent / 'none.db'))

    def test_keychain_and_db_resolution(self):
        with patch.dict(os.environ, {'CURSOR_SESSION_TOKEN': jwt()}):
            token, source = c._credentials()
            self.assertTrue(source.startswith('CURSOR_SESSION_TOKEN'))
            self.assertTrue(token.startswith('user_ABC::'))
        with patch.dict(os.environ, {'CURSOR_SESSION_TOKEN': ''}):
            with patch.object(c, '_keychain_token', return_value=jwt()):
                self.assertIn('Keychain', c._credentials()[1])

    def test_legacy_summary(self):
        data = {'membershipType': 'pro', 'billingCycleEnd': '2026-11-01T00:00:00Z',
                'individualUsage': {'plan': {'enabled': True, 'used': 800,
                                              'limit': 2000, 'autoPercentUsed': 22.5,
                                              'apiPercentUsed': 45}}}
        parsed = c.parse_usage(data)
        self.assertEqual(parsed['included_used_percent'], 40)
        self.assertEqual(parsed['other_models_percent'], 45)
        self.assertEqual(parsed['quota_type'], 'billing_cycle')

    def test_current_period_prefers_derived_total_spend(self):
        data = {'billingCycleEnd': '1794000000000',
                'planUsage': {'includedSpend': 1288, 'totalSpend': 1288, 'limit': 2000,
                              'totalPercentUsed': 3.7333, 'autoPercentUsed': 3.89,
                              'apiPercentUsed': 2.64}}
        parsed = c.parse_usage({}, data)
        self.assertEqual(parsed['included_used_percent'], 64.4)
        self.assertEqual(parsed['cursor_models_percent'], 3.89)
        self.assertEqual(parsed['quota_type'], 'billing_cycle')
        self.assertNotEqual(parsed['included_used_percent'], 3.73)

    def test_unknown_x_subscription_does_not_invent_quota(self):
        parsed = c.parse_usage({'membershipType': 'premium', 'weeklyUsage': {'remaining': 123}})
        self.assertEqual(parsed['status'], 'unavailable')
        self.assertNotIn('included_used_percent', parsed)

    def test_no_zero_when_missing(self):
        parsed = c.parse_usage({'individualUsage': {'plan': {'enabled': True}}}, {})
        self.assertEqual(parsed['status'], 'unavailable')

    def test_probe_requests_signed_in_data_and_never_returns_token(self):
        calls = []
        def fake_request(path, cookie, *, body=None, timeout=8):
            calls.append((path, body))
            self.assertEqual(cookie, 'user_ABC::' + jwt())
            if path.endswith('usage-summary'):
                return {'billingCycleEnd': '2026-11-01', 'individualUsage': {'plan': {'used': 1200, 'limit': 2000}}}
            if path.endswith('/me'):
                return {'id': 123, 'email': 'do-not-print@example.com'}
            if path.endswith('/get-current-period-usage'):
                return {'planUsage': {'includedSpend': 300, 'limit': 2000, 'autoPercentUsed': 44}}
            raise AssertionError(path)
        with patch.object(c, '_credentials', return_value=('user_ABC::'+jwt(), 'test session')):
            with patch.object(c, '_request', side_effect=fake_request):
                parsed = c.probe()
        self.assertEqual(parsed['status'], 'ok')
        self.assertEqual(parsed['included_used_percent'], 15)
        self.assertEqual(len(calls), 3)
        self.assertNotIn('email', json.dumps(parsed))
        self.assertNotIn(jwt(), json.dumps(parsed))

    def test_cookie_sent_only_to_cursor_dashboard_and_no_redirects(self):
        reqs = []
        def fake_open(req, timeout=8):
            reqs.append(req)
            return io.BytesIO(b'{"planUsage":{"limit":2000,"includedSpend":100}}')
        with patch.object(c._HTTP, 'open', side_effect=fake_open):
            res = c._request('/api/dashboard/get-current-period-usage', 'user_ABC::' + jwt(), body={'userId': 42})
        self.assertEqual(res['planUsage']['limit'], 2000)
        self.assertEqual(reqs[0].full_url, 'https://cursor.com/api/dashboard/get-current-period-usage')
        self.assertEqual(reqs[0].get_method(), 'POST')
        self.assertEqual(reqs[0].get_header('Origin'), 'https://cursor.com')
        self.assertIn('WorkosCursorSessionToken=', reqs[0].get_header('Cookie'))
        self.assertIsNone(c._NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://other.example'))
        with self.assertRaises(ValueError):
            c._request('https://evil.example/api/usage-summary', 'test')

    def test_render_and_compact(self):
        x = c.parse_usage({'billingCycleEnd': '2026-11-01T00:00:00Z', 'individualUsage': {'plan': {'used': 1100, 'limit': 2000,
                                                                                                 'autoPercentUsed': 50}}})
        x['session_source'] = 'Cursor app'
        snap = {'checked_at': '2026-10-17T00:00:00Z', 'providers': {'cursor': x}}
        self.assertIn('55.0%', o.render(snap))
        self.assertIn('Cursor models', o.render(snap))
        self.assertIn('55%', o.compact(snap))


if __name__ == '__main__':
    unittest.main()
