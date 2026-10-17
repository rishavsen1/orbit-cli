#!/usr/bin/env python3
"""Read-only Antigravity quotas, preferring authenticated `agy -p /usage`.

Sources in order:
  1. The installed agy CLI's structured /usage report (requires an existing login).
  2. The already-running Antigravity IDE's localhost language server.

No agent prompt is sent and no remote OAuth credentials are handled by this module.
Only normalized percentages, reset timestamps and plan/source labels are returned.
The local language-server protocol and agy's report schema may change.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import urllib.request

ENDPOINT = '/exa.language_server_pb.LanguageServerService/'
PAYLOAD = json.dumps({'metadata': {
    'ideName': 'antigravity', 'extensionName': 'antigravity',
    'ideVersion': 'unknown', 'locale': 'en',
}}).encode('utf-8')
AGY_TIMEOUT = 45
MAX_JSON = 1024 * 1024


def _run(cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def find_agy():
    """Explicit path wins; otherwise PATH then normal user installation paths."""
    explicit = os.environ.get('ORBIT_AGY_PATH') or os.environ.get('ANTIGRAVITY_CLI_PATH') or os.environ.get('AGY_BIN')
    if explicit:
        resolved = shutil.which(explicit) if os.sep not in explicit else str(Path(explicit).expanduser())
        return resolved if resolved and Path(resolved).is_file() and os.access(resolved, os.X_OK) else None
    found = shutil.which('agy')
    if found:
        return found
    candidates = [Path.home() / '.local/bin/agy', Path('/opt/homebrew/bin/agy'),
                  Path('/usr/local/bin/agy')]
    if sys.platform == 'win32':
        candidates += [Path(os.environ.get('LOCALAPPDATA', '')) / 'agy/bin/agy.exe',
                       Path.home() / 'AppData/Local/agy/bin/agy.exe']
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


def agy_has_profile():
    """Avoid starting agy on fresh accounts, where it may launch browser OAuth."""
    return (Path.home() / '.gemini/antigravity-cli').exists()


def _block_browser(temp: Path, env: dict):
    """Best effort: don't launch a browser if the saved login has expired."""
    if sys.platform == 'win32':
        program = temp / 'orbit-no-browser.cmd'
        program.write_text('@echo off\r\nexit /b 1\r\n', encoding='utf-8')
    else:
        program = temp / 'orbit-no-browser'
        program.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        program.chmod(0o700)
        for command in ('open', 'xdg-open'):
            helper = temp / command
            helper.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
            helper.chmod(0o700)
    env['BROWSER'] = str(program)
    env['PATH'] = str(temp) + os.pathsep + env.get('PATH', '')


def _read_agy_report(binary: str):
    """Run ONLY the CLI slash command, never an inference prompt.

    Avoid unchecked long-running sign-in prompts. Use a fresh working directory,
    bounded timeout and group-owned process cleanup. No raw CLI output is logged.
    """
    if not agy_has_profile() and os.environ.get('ORBIT_AGY_ALLOW_UNVERIFIED') != '1':
        raise RuntimeError('agy profile not found; sign in once using `agy` in a terminal')
    with tempfile.TemporaryDirectory(prefix='orbit-agy-') as tmp:
        workdir = Path(tmp)
        env = dict(os.environ)
        _block_browser(workdir, env)
        cmd = [binary, '-p', '/usage', '--output-format', 'json']
        kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.PIPE,
                  'stderr': subprocess.PIPE, 'cwd': tmp, 'env': env}
        if sys.platform != 'win32':
            kwargs['start_new_session'] = True
        try:
            p = subprocess.Popen(cmd, **kwargs)
        except OSError:
            raise RuntimeError('Could not launch agy CLI') from None
        try:
            output, errors = p.communicate(timeout=AGY_TIMEOUT)
        except subprocess.TimeoutExpired:
            if sys.platform != 'win32':
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                p.kill()
            p.communicate()
            raise RuntimeError('agy /usage timed out; check login or network') from None
        if len(output) > MAX_JSON:
            raise RuntimeError('agy returned an unexpectedly large usage report')
        if p.returncode != 0:
            text = (errors + output).decode('utf-8', errors='replace')[:8000]
            if re.search(r'login|log in|sign.in|auth|oauth|credential', text, re.I):
                raise RuntimeError('agy login expired; run `agy` interactively once')
            raise RuntimeError('agy /usage exited unsuccessfully; run `agy -p /usage --output-format json` to diagnose')
        try:
            report = json.loads(output)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError('agy returned non-JSON /usage output; upgrade the agy CLI') from None
        return parse_agy_report(report)


def _fraction(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        x = float(value)
        return x if math.isfinite(x) and 0 <= x <= 1 else None
    except (TypeError, ValueError):
        return None


def _bucket_window(bucket):
    # Prefer explicit cadence; never fabricate a five-hour window.
    raw = ' '.join(str(bucket.get(k) or '') for k in
                   ('window', 'windowMinutes', 'id', 'bucketId', 'bucket_id', 'displayName', 'display_name')).lower()
    if re.search(r'\bweekly\b|\b7[ _-]?d(?:ays?)?\b|\b604800\b', raw):
        return '7 days'
    if re.search(r'\b5[ _-]?h(?:ours?)?\b|\b5[ _-]?hour\b|\b300\b', raw):
        return '5 hours'
    return None


def _report_groups(report):
    if not isinstance(report, dict):
        return None
    if str(report.get('status', 'SUCCESS')).upper() not in ('SUCCESS', 'OK'):
        return None
    command = report.get('command') or {}
    if isinstance(command, dict):
        data = command.get('data') or {}
        if isinstance(data, dict) and isinstance(data.get('groups'), list):
            return data['groups']
    for data in (report.get('data'), report.get('response'), report):
        if isinstance(data, dict) and isinstance(data.get('groups'), list):
            return data['groups']
    return None


def parse_agy_report(report):
    """Normalize agy's quota-summary JSON. Reject chat results and unknown windows."""
    groups = _report_groups(report)
    if groups is None:
        raise RuntimeError('agy response did not contain a structured /usage report')
    models = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        title = str(group.get('displayName') or group.get('display_name') or
                    group.get('name') or group.get('id') or 'Antigravity')
        title = re.sub(r'\s+', ' ', title).strip()[:65]
        for bucket in group.get('buckets') or []:
            if not isinstance(bucket, dict) or bucket.get('enabled') is False:
                continue
            if str(bucket.get('status') or '').lower() in ('disabled', 'unavailable'):
                continue
            remaining = bucket.get('remaining_fraction', bucket.get('remainingFraction'))
            if remaining is None:
                nested = bucket.get('remaining') or {}
                if isinstance(nested, dict):
                    remaining = nested.get('remainingFraction', nested.get('remaining_fraction'))
            fraction = _fraction(remaining)
            if fraction is None:
                continue  # an absent fraction means unknown, not 0% remaining
            cadence = _bucket_window(bucket)
            if cadence is None:
                # Future quota windows should not be misidentified as 5h or weekly.
                continue
            models.append({'name': f'{title} · {cadence}', 'remaining': fraction,
                           'resetAt': (bucket.get('reset_time') or bucket.get('resetTime') or
                                       (bucket.get('remaining') or {}).get('resetTime') or '')})
    if not models:
        raise RuntimeError('agy /usage provided no enabled, measured 5-hour or weekly quota windows')
    return {'models': models, 'source': 'agy CLI /usage', 'grouped': True, 'plan': ''}


def processes():
    """Yield Antigravity local desktop language-server process IDs and arguments."""
    if sys.platform == 'win32':
        script = ("Get-CimInstance Win32_Process | Where-Object "
                  "{$_.Name -match 'language.server'} | "
                  "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        try:
            payload = json.loads(_run(['powershell', '-NoProfile', '-Command', script]))
            for x in payload if isinstance(payload, list) else [payload]:
                cmd = x.get('CommandLine') or ''
                if '--csrf_token' in cmd and ('antigravity' in cmd.lower() or 'jetski' in cmd.lower()):
                    yield str(x['ProcessId']), cmd
        except (ValueError, TypeError, KeyError):
            return
    else:
        for line in _run(['ps', '-axo', 'pid=,command=']).splitlines():
            m = re.match(r'^\s*(\d+)\s+(.*)$', line)
            if not m:
                continue
            cmd = m.group(2)
            if ('language_server' in cmd.lower() or 'language-server' in cmd.lower()) and \
                    '--csrf_token' in cmd and ('antigravity' in cmd.lower() or 'jetski' in cmd.lower()):
                yield m.group(1), cmd


def get_token(cmd):
    match = re.search(r'--(?:extension_server_)?csrf_token(?:=|\s+)([A-Za-z0-9_\-]+)', cmd)
    return match.group(1) if match else None


def ports(pid, cmd):
    possible = []
    for match in re.finditer(r'--extension_server_port(?:=|\s+)(\d+)', cmd):
        possible.append(int(match.group(1)))
    if sys.platform == 'win32':
        script = (f'Get-NetTCPConnection -State Listen -OwningProcess {int(pid)} '
                  '-ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort')
        output = _run(['powershell', '-NoProfile', '-Command', script])
        possible.extend(int(p) for p in output.split() if p.isdigit())
    else:
        output = _run(['lsof', '-nP', '-a', '-p', str(pid), '-iTCP', '-sTCP:LISTEN'])
        possible.extend(int(p) for p in re.findall(r':(\d+)\s*\(LISTEN\)', output))
    return list(dict.fromkeys(p for p in possible if 1024 <= p <= 65535))[:16]


def fetch_status(port, csrf):
    headers = {'Content-Type': 'application/json', 'Connect-Protocol-Version': '1',
               'x-codeium-csrf-token': csrf}
    # Local Antigravity currently supports HTTPS with a self-signed cert.
    # TLS verification bypass is scoped STRICTLY to 127.0.0.1, never the network.
    for scheme in ('https', 'http'):
        for method in ('RetrieveUserQuotaSummary', 'GetUserStatus', 'GetCommandModelConfigs'):
            req = urllib.request.Request(
                f'{scheme}://127.0.0.1:{port}{ENDPOINT}{method}',
                data=PAYLOAD, headers=headers, method='POST')
            try:
                # Never permit a proxy: the CSRF header is strictly loopback-only.
                handlers = [urllib.request.ProxyHandler({})]
                if scheme == 'https':
                    handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
                opener = urllib.request.build_opener(*handlers)
                with opener.open(req, timeout=1.5) as resp:
                    raw = resp.read(MAX_JSON + 1)
                if len(raw) > MAX_JSON:
                    continue
                doc = json.loads(raw)
                if not isinstance(doc, dict):
                    continue
                if method == 'RetrieveUserQuotaSummary':
                    # It may have the same quota groups as `agy /usage`.
                    try:
                        parsed = parse_agy_report({'command': {'data': doc}})
                        parsed['source'] = 'Antigravity local quota summary'
                        return parsed
                    except RuntimeError:
                        continue
                models = sanitize_legacy(doc)
                if models:
                    return {'models': models, 'source': 'Antigravity IDE local service',
                            'grouped': False, 'plan': ''}
            except Exception:
                continue
    return None


def sanitize_legacy(doc):
    info = doc.get('userStatus') or {}
    configs = (info.get('cascadeModelConfigData') or {}).get('clientModelConfigs') or []
    if not configs:
        configs = doc.get('clientModelConfigs') or (doc.get('cascadeModelConfigData') or {}).get('clientModelConfigs') or []
    models = []
    for c in configs:
        if not isinstance(c, dict):
            continue
        quota = c.get('quotaInfo') or {}
        fraction = _fraction(quota.get('remainingFraction'))
        if fraction is None:
            continue
        models.append({'name': str(c.get('label') or 'Unnamed model')[:100],
                       'remaining': fraction, 'resetAt': str(quota.get('resetTime') or '')[:50]})
    return models


def local_usage():
    found = False
    for pid, cmd in processes():
        found = True
        token = get_token(cmd)
        if not token:
            continue
        for port in ports(pid, cmd):
            result = fetch_status(port, token)
            if result:
                return result
    if not found:
        raise RuntimeError('Antigravity IDE language server not running')
    raise RuntimeError('Antigravity IDE local quota endpoint unavailable')


def diagnostics():
    """Credential-free and non-network diagnostic; safe to paste into an issue."""
    binary = find_agy()
    visible = binary.replace(str(Path.home()), '~', 1) if binary else None
    return {'agy_found': bool(binary), 'agy_executable': visible,
            'agy_profile_found': agy_has_profile(),
            'agy_version': _run([binary, '--version'], 4).strip()[:80] if binary else None,
            'ide_language_servers': len(list(processes()))}


def get_usage():
    problems = []
    binary = find_agy()
    if binary:
        try:
            return _read_agy_report(binary)
        except RuntimeError as exc:
            problems.append('agy: ' + str(exc))
    else:
        problems.append('agy CLI not found (install it or set ORBIT_AGY_PATH)')
    try:
        return local_usage()
    except RuntimeError as exc:
        problems.append('IDE: ' + str(exc))
    raise RuntimeError('; '.join(problems))
