#!/usr/bin/env python3
"""Read-only account-plan usage from the signed-in Cursor IDE / cursor-agent.

Uses Cursor's *unofficial* dashboard APIs (no public individual-plan API).
The authenticated JWT is read only into process memory from:
  CURSOR_SESSION_TOKEN -> macOS cursor-agent Keychain -> Cursor IDE state.vscdb.
Never prints, persists, or transmits a session outside https://cursor.com.
No model requests are made; local CLI `status` is not mistaken for usage.
"""
from __future__ import annotations

import base64
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = 'https://cursor.com'
USER_AGENT = 'orbit-usage/0.3 (read-only Cursor personal usage)'
MAX_JSON = 500_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirected request must never forward a signed-in session cookie.
        return None


_HTTP = urllib.request.build_opener(_NoRedirect())
DB_KEY = 'cursorAuth/accessToken'


def _finite(value):
    try:
        if value is None or isinstance(value, bool) or str(value).strip() == '':
            return None
        x = float(value)
        return x if x == x and x not in (float('inf'), float('-inf')) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _pct(value):
    x = _finite(value)
    return None if x is None else round(max(0.0, min(100.0, x)), 2)


def _date(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            n = float(value)
            when = dt.datetime.fromtimestamp(n / (1000 if n > 1e11 else 1), tz=dt.timezone.utc)
        else:
            when = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            when = when.replace(tzinfo=dt.timezone.utc) if when.tzinfo is None else when.astimezone(dt.timezone.utc)
        return when.isoformat().replace('+00:00', 'Z')
    except (OSError, TypeError, ValueError, OverflowError):
        return None


def _session_token(raw: str) -> str | None:
    """Convert Cursor's JWT or signed-in web cookie to the expected cookie value.

    Never trust a fabricated sub: derive it from JWT, using only a valid session
    with a sub claim. The signed JWT itself is never decoded as a source of usage.
    """
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw.strip().strip('"').strip("'")
    if candidate.startswith('WorkosCursorSessionToken='):
        candidate = candidate.split('=', 1)[1].split(';', 1)[0]
    candidate = urllib.parse.unquote(candidate)
    jwt = candidate.split('::', 1)[-1] if '::' in candidate else candidate
    if jwt.count('.') != 2 or len(jwt) > 16_000:
        return None
    try:
        segment = jwt.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))
        sub = payload.get('sub')
        if not isinstance(sub, str) or not (2 <= len(sub) <= 250) or not re.fullmatch(r'[\w|:@.\-]+', sub):
            return None
        # A value read from the IDE may already be a cookie. Still regenerate
        # sub from the JWT to avoid an untrusted prefix.
        return f'{sub}::{jwt}'
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def _keychain_token():
    if sys.platform != 'darwin':
        return None
    try:
        proc = subprocess.run(['security', 'find-generic-password', '-s', 'cursor-access-token', '-w'],
                              capture_output=True, text=True, timeout=5, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _db_paths():
    if sys.platform == 'darwin':
        folder = Path.home() / 'Library/Application Support/Cursor/User/globalStorage'
    elif sys.platform == 'win32':
        folder = Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming'))) / 'Cursor/User/globalStorage'
    else:
        folder = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'Cursor/User/globalStorage'
    return [folder / 'state.vscdb']


def _database_token(path: Path):
    if not path.is_file():
        return None
    # Open without modification. Using mode=ro preserves visibility of
    # committed SQLite WAL state, unlike immutable=1.
    uri = 'file:' + urllib.parse.quote(str(path.resolve()), safe='/') + '?mode=ro'
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            row = db.execute('SELECT value FROM ItemTable WHERE key = ?', (DB_KEY,)).fetchone()
        if row and isinstance(row[0], str):
            return row[0]
    except (OSError, sqlite3.Error):
        pass
    return None


def _credentials():
    explicit = os.environ.get('CURSOR_SESSION_TOKEN')
    if explicit:
        token = _session_token(explicit)
        if not token:
            raise RuntimeError('CURSOR_SESSION_TOKEN is set but not a recognized Cursor session')
        return token, 'CURSOR_SESSION_TOKEN (manual override)'
    raw = _keychain_token()
    token = _session_token(raw) if raw else None
    if token:
        return token, 'Existing cursor-agent macOS Keychain session'
    for path in _db_paths():
        raw = _database_token(path)
        token = _session_token(raw) if raw else None
        if token:
            return token, 'Existing Cursor IDE signed-in session (read-only SQLite)'
    raise RuntimeError('Cursor session not found. Sign into Cursor IDE (or Cursor CLI on macOS), then retry.')


def _request(path, cookie, *, body=None, timeout=8):
    if not path.startswith('/api/') or '\\' in path or '?' in path:
        raise ValueError('Invalid Cursor API path')
    url = BASE_URL + path
    header = {'Cookie': 'WorkosCursorSessionToken=' + urllib.parse.quote(cookie, safe=''),
              'Accept': 'application/json', 'User-Agent': USER_AGENT}
    if body is not None:
        header.update({'Content-Type': 'application/json', 'Origin': BASE_URL})
        request_body = json.dumps(body, separators=(',', ':')).encode('utf-8')
        method = 'POST'
    else:
        request_body = None
        method = 'GET'
    request = urllib.request.Request(url, data=request_body, headers=header, method=method)
    try:
        with _HTTP.open(request, timeout=timeout) as response:
            payload = response.read(MAX_JSON + 1)
            if len(payload) > MAX_JSON:
                raise RuntimeError('Cursor returned an unexpectedly large response')
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise RuntimeError('Cursor returned an unexpected response format')
        return parsed
    except urllib.error.HTTPError as exc:
        # Do not ever print response headers/body: they might include secrets.
        if exc.code in (401, 403):
            raise RuntimeError('Cursor dashboard session expired or rejected (HTTP %d)' % exc.code) from None
        raise RuntimeError('Cursor dashboard returned HTTP %d' % exc.code) from None
    except urllib.error.URLError:
        raise RuntimeError('Cursor dashboard unreachable') from None
    except TimeoutError:
        raise RuntimeError('Cursor dashboard timed out') from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError('Cursor dashboard returned non-JSON data') from None


def _percent_or_ratio(obj: dict | None):
    if not isinstance(obj, dict):
        return None
    # Prefer derived ratio for total allowance, not backend totalPercentUsed;
    # Cursor's fields may represent different internal measures.
    used = _finite(obj.get('includedSpend', obj.get('used', obj.get('totalSpend'))))
    limit = _finite(obj.get('limit'))
    if used is not None and limit is not None and limit > 0:
        return _pct(100 * used / limit)
    return _pct(obj.get('totalPercentUsed', obj.get('usedPercent')))


def parse_usage(summary, period=None):
    """Normalize only known Cursor dashboard schema fields.

    Do not assume personal plans have 5-hour quotas. Legacy is billing-cycle;
    new X-subscription weekly pools may require a separate provider dashboard.
    """
    s = summary if isinstance(summary, dict) else {}
    p = period if isinstance(period, dict) else {}
    period_plan = p.get('planUsage') if isinstance(p.get('planUsage'), dict) else {}
    individual = s.get('individualUsage') if isinstance(s.get('individualUsage'), dict) else {}
    summary_plan = individual.get('plan') if isinstance(individual.get('plan'), dict) else {}

    # A dashboard can expose two different percent metrics, and neither is
    # equal to includedSpend/limit. Keep those labeled independently.
    pool = period_plan or summary_plan
    used = _finite(pool.get('includedSpend', pool.get('used', pool.get('totalSpend'))))
    limit = _finite(pool.get('limit'))
    result = {'status': 'ok', 'plan': s.get('membershipType') if isinstance(s.get('membershipType'), str) else None,
              'source': 'Cursor signed-in web dashboard (unofficial API)',
              'quota_type': 'billing_cycle' if _date(p.get('billingCycleEnd') or s.get('billingCycleEnd')) else 'unknown',
              'resets_at': _date(p.get('billingCycleEnd') or s.get('billingCycleEnd')),
              'included_used_percent': _percent_or_ratio(pool),
              'cursor_models_percent': _pct(pool.get('autoPercentUsed')),
              'other_models_percent': _pct(pool.get('apiPercentUsed')),
              'included_used_units': used, 'included_limit_units': limit,
              'on_demand_used_units': _finite((individual.get('onDemand') or {}).get('used'))
              if isinstance(individual.get('onDemand'), dict) else None,
              'note': 'Account-wide usage (IDE + CLI), not 5-hour usage. Cursor dashboard APIs are unofficial.'}
    known = any(result[k] is not None for k in ('included_used_percent', 'cursor_models_percent',
                                                 'other_models_percent', 'on_demand_used_units'))
    if not known:
        return {'status': 'unavailable', 'note': 'Cursor dashboard did not expose a recognized quota; if on X Subscription, weekly quota may only be shown at grok.com/dashboard.'}
    return result


def probe():
    cookie, auth_source = _credentials()
    errors = []
    try:
        summary = _request('/api/usage-summary', cookie)
    except RuntimeError as exc:
        summary = {}
        errors.append(str(exc))
    period = {}
    try:
        who = _request('/api/auth/me', cookie)
        user_id = who.get('id')
        if isinstance(user_id, (int, str)) and str(user_id).isdigit():
            # Use rolling interval merely to constrain dashboard query, not
            # as a guess about subscription renewal.
            now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
            start = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).timestamp() * 1000)
            period = _request('/api/dashboard/get-current-period-usage', cookie,
                              body={'teamId': 0, 'userId': int(user_id),
                                    'startDate': str(start), 'endDate': str(now)})
    except RuntimeError as exc:
        errors.append(str(exc))
    normalized = parse_usage(summary, period)
    normalized['session_source'] = auth_source
    if normalized['status'] != 'ok':
        reason = ' / '.join(dict.fromkeys(errors))
        if reason:
            normalized['note'] += ' ' + reason
        return normalized
    if errors:
        normalized['note'] += ' Some dashboard endpoints unavailable.'
    return normalized


def diagnostics():
    """Checks local source availability only. Does not expose authentication."""
    return {'cursor_cli_found': bool(shutil.which('cursor-agent') or shutil.which('agent')),
            'cursor_env_session_set': bool(os.environ.get('CURSOR_SESSION_TOKEN')),
            'cursor_ide_db_found': any(path.is_file() for path in _db_paths()),
            'macos_keychain_supported': sys.platform == 'darwin',
            'note': 'No credentials printed. This diagnostic makes no HTTP requests.'}
