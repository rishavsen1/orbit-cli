"""Read Kiro CLI account quotas over local ACP; optionally tally local CLI credit history.

No LLM prompts, direct AWS calls, credentials, logs, or writes. Uses the user's
already-authenticated ``kiro-cli`` executable. Account usage is authoritative;
local session activity only measures credits recorded *on this computer*.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
from datetime import datetime, timezone, timedelta


class KiroUnavailable(RuntimeError):
    pass


def finite(v):
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def date_string(value):
    """Return ISO timestamp or date as supplied by Kiro, after validation."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value
    except ValueError:
        return None


def extract_quota(payload):
    """Keep only subscription credits from ACP; never return raw account data."""
    if not isinstance(payload, dict):
        raise KiroUnavailable('Kiro did not return a usage object')
    # Both V3 and V2 adapters can wrap the command result.
    for _ in range(4):
        if isinstance(payload, dict) and isinstance(payload.get('usageBreakdowns'), list):
            break
        nested = next((payload.get(k) for k in ('data', 'usage', 'result')
                       if isinstance(payload.get(k), dict)), None)
        if nested is None:
            break
        payload = nested
    if not isinstance(payload.get('usageBreakdowns'), list):
        raise KiroUnavailable('Kiro usage data unavailable (unsupported CLI response)')
    candidates = []
    for item in payload['usageBreakdowns']:
        if not isinstance(item, dict) or item.get('hasLimit') is False:
            continue
        used, limit = finite(item.get('used')), finite(item.get('limit'))
        if used is None or limit is None or limit <= 0:
            continue
        name = str(item.get('displayName') or item.get('resourceType') or '')
        # Prefer the main subscription credits rather than add-ons/bonus pools.
        preference = 2 if 'credit' in name.lower() else 1
        if 'bonus' in name.lower() or 'add-on' in name.lower():
            preference = 0
        candidates.append((preference, item, used, limit, name))
    if not candidates:
        raise KiroUnavailable('Kiro account has no finite subscription credit limit')
    _, item, used, limit, name = sorted(candidates, key=lambda x: -x[0])[0]
    reset = date_string(payload.get('billingCycleReset') or item.get('resetsAt'))
    return {
        'used': round(used, 4), 'limit': round(limit, 4),
        'resetAt': reset, 'plan': str(payload.get('planName') or '')[:60],
        'resource': name[:50], 'overagesEnabled': payload.get('overagesEnabled') is True,
        'source': 'kiro-cli ACP',
    }


class RpcClient:
    """One short-lived ACP session with bounded replies and guaranteed cleanup."""
    def __init__(self, argv, timeout=11):
        self.argv = argv
        self.timeout = timeout
        self.proc = None
        self.messages = queue.Queue()
        self.seq = 0

    def __enter__(self):
        self.proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
            bufsize=1, cwd=str(Path.home()),
        )
        def reader():
            # Limit response sizes, avoid retaining any raw stdout in memory once used.
            # Parent may close stdout during __exit__; treat that as a normal shutdown.
            try:
                stdout = self.proc.stdout
                if stdout is None:
                    return
                for line in stdout:
                    if len(line) > 1_000_000:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict):
                            self.messages.put(data)
                    except (ValueError, TypeError):
                        pass
            except (ValueError, OSError):
                pass
            finally:
                self.messages.put(None)
        self._reader = threading.Thread(target=reader, daemon=True)
        self._reader.start()
        return self

    def __exit__(self, *_):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=1)
            # Close stdin first so the child exits; join the reader before closing
            # stdout so the thread does not race on a closed pipe.
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            reader = getattr(self, "_reader", None)
            if reader is not None:
                reader.join(timeout=1.5)
            if self.proc.stdout:
                try:
                    self.proc.stdout.close()
                except OSError:
                    pass

    def request(self, method, params):
        self.seq += 1
        rid = self.seq
        msg = {'jsonrpc':'2.0', 'id':rid, 'method':method, 'params':params}
        try:
            self.proc.stdin.write(json.dumps(msg, separators=(',', ':')) + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise KiroUnavailable('Kiro ACP terminated during request')
        import time
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                data = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if data is None:
                raise KiroUnavailable('Kiro ACP closed without usage data')
            if data.get('id') != rid:
                # Never respond to unsolicited server requests; we only query read-only metadata.
                continue
            if 'error' in data:
                raise KiroUnavailable('Kiro ACP does not support this usage request')
            return data.get('result')
        raise KiroUnavailable('Kiro ACP timed out. Check CLI login and version')


def cli_binary():
    custom = os.environ.get('ORBIT_KIRO_BIN')
    if custom:
        p = Path(custom).expanduser()
        if not p.is_file():
            raise KiroUnavailable('ORBIT_KIRO_BIN does not point to a file')
        return str(p)
    found = shutil.which('kiro-cli')
    if found:
        return found
    # CLI installed as macOS .app but not visible on PATH.
    fallback = Path('/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli-chat')
    if fallback.exists():
        return str(fallback)
    raise KiroUnavailable('Kiro CLI not found. Install it and sign in with kiro-cli login')


def account_usage():
    binary = cli_binary()
    # Official V3 typed extension, called ONLY if advertised by initialize.
    try:
        with RpcClient([binary, 'acp', '--agent-engine=v3'], timeout=9) as client:
            hello = client.request('initialize', {
                'protocolVersion':1, 'clientCapabilities':{},
                'clientInfo':{'name':'orbit-quotas','version':'0.2.0'}
            })
            caps = (hello or {}).get('agentCapabilities') or {}
            meta = caps.get('_meta') or {}
            extensions = (meta.get('kiro') or {}).get('extensionMethods') or []
            advertised = [m if isinstance(m, str) else (m.get('name') or m.get('method'))
                          for m in extensions if isinstance(m, (str, dict))]
            if '_kiro/account/getUsage' not in advertised:
                raise KiroUnavailable('Kiro V3 did not advertise account usage')
            return extract_quota(client.request('_kiro/account/getUsage', {}))
    except (KiroUnavailable, OSError, ValueError, AttributeError) as v3error:
        # Legacy V2 adapter. Never send V2 command to V3; it was removed.
        try:
            with RpcClient([binary, 'acp', '--agent-engine=v2'], timeout=9) as client:
                client.request('initialize', {
                    'protocolVersion':1, 'clientCapabilities':{},
                    'clientInfo':{'name':'orbit-quotas','version':'0.2.0'}
                })
                session = client.request('session/new', {'cwd':str(Path.home()),'mcpServers':[]})
                sid = (session or {}).get('sessionId')
                if not sid:
                    raise KiroUnavailable('Kiro V2 did not return a session')
                raw = client.request('_kiro.dev/commands/execute', {
                    'sessionId':sid, 'command':{'command':'usage','args':{}}
                })
                return extract_quota(raw)
        except (KiroUnavailable, OSError, ValueError, AttributeError) as v2error:
            raise KiroUnavailable(
                'Kiro account quota unavailable via ACP (V3 or V2). '
                'Update Kiro CLI and verify login.'
            ) from v2error


def parse_time(stamp):
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace('Z','+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def local_activity(now=None, session_dir=None):
    """Exact recorded CLI credits for rolling windows; NEVER a quota percentage."""
    now = now or datetime.now(timezone.utc)
    directory = Path(session_dir or os.environ.get('KIRO_SESSIONS_DIR') or Path.home() / '.kiro/sessions/cli')
    if not directory.is_dir():
        return {'available':False, 'complete':False, 'reason':'No local CLI sessions found'}
    cutoff7 = now - timedelta(days=7)
    cutoff5 = now - timedelta(hours=5)
    total5 = total7 = 0.0
    matched = scanned = invalid = 0
    try:
        paths = directory.glob('*.json')
        for path in paths:
            if scanned >= 3000:
                invalid += 1
                break
            # Files not updated in the last 7 days cannot contain recent turn data.
            try:
                if path.stat().st_mtime < cutoff7.timestamp():
                    continue
                if path.stat().st_size > 16_000_000:
                    invalid += 1
                    continue
                with path.open('r', encoding='utf-8') as f:
                    data = json.load(f)
                scanned += 1
                turns = (((data.get('session_state') or {}).get('conversation_metadata') or {})
                         .get('user_turn_metadatas') or [])
                if not isinstance(turns, list):
                    continue
                for turn in turns:
                    if not isinstance(turn, dict):
                        continue
                    ts = parse_time(turn.get('end_timestamp'))
                    if ts is None or not (cutoff7 <= ts <= now + timedelta(minutes=1)):
                        continue
                    metering = turn.get('metering_usage') or []
                    if not isinstance(metering, list):
                        continue
                    values = [finite(x.get('value')) for x in metering
                              if isinstance(x,dict) and x.get('unit') == 'credit']
                    values = [v for v in values if v is not None]
                    if values:
                        matched += 1
                        spent = sum(values)
                        total7 += spent
                        if ts >= cutoff5:
                            total5 += spent
            except (OSError, ValueError, TypeError, AttributeError):
                invalid += 1
    except OSError:
        invalid += 1
    return {
        'available': matched > 0, 'complete':False,
        'shortCredits': round(total5, 3) if matched else None,
        'weekCredits': round(total7, 3) if matched else None,
        'turns':matched,
        'reason':'Local CLI history only; excludes other devices, missing sessions and IDE activity',
        'partial':invalid > 0,
    }


def get_kiro_usage():
    # Let account quota failure propagate. Do not show local spending as full account data.
    quota = account_usage()
    return {'quota':quota, 'activity':local_activity()}


if __name__ == '__main__':
    try:
        print(json.dumps(get_kiro_usage(), indent=2))
    except KiroUnavailable as exc:
        print(f'Kiro unavailable: {exc}', file=__import__('sys').stderr)
        raise SystemExit(2)
