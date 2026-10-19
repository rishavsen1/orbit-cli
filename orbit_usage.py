#!/usr/bin/env python3
"""Orbit Usage: one-command read-only local-first AI subscription usage dashboard.

Python 3.10+ standard library. No passwords, browser extension, local web server,
package installation, or model inference required. Uses signed-in CLI sessions.
Note: some data sources use unofficial provider interfaces and may change.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

UTC = dt.timezone.utc
PROVIDERS = ("claude", "codex", "antigravity", "kiro", "cursor")

# Single fixed cache file in the system temp dir. It is overwritten in place on
# every refresh (atomic replace), so it never grows or accumulates stale copies.
import tempfile

CACHE_PATH = Path(tempfile.gettempdir()) / "orbit-usage-cache.json"


def read_cache(selected, max_age):
    """Return a cached snapshot if it is fresh and matches the selected providers.

    max_age is in seconds; <= 0 disables the cache. Any read error returns None
    so Orbit silently falls back to a live collection.
    """
    if max_age is None or max_age <= 0:
        return None
    try:
        age = time.time() - CACHE_PATH.stat().st_mtime
        if age > max_age:
            return None
        with CACHE_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        # Only reuse a cache built for exactly the same provider set.
        if payload.get("_providers") != list(selected):
            return None
        return payload.get("snapshot")
    except (OSError, ValueError):
        return None


def write_cache(selected, snap):
    """Overwrite the single cache file atomically (temp file + os.replace).

    No new files accumulate: the temp file is renamed onto CACHE_PATH, and on any
    failure the temp file is removed. Writing is best-effort and never fatal.
    """
    payload = {"_providers": list(selected), "snapshot": snap}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), prefix=".orbit-usage-cache-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)  # atomic overwrite; tmp no longer exists
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def iso(t=None):
    return (t or dt.datetime.now(UTC)).isoformat().replace("+00:00", "Z")


def parse_dt(v):
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            n = float(v)
            return dt.datetime.fromtimestamp(n / (1000 if n > 10**11 else 1), tz=UTC)
        p = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return p.replace(tzinfo=UTC) if p.tzinfo is None else p.astimezone(UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def number(v):
    try:
        if v is None or isinstance(v, bool) or v == "":
            return None
        x = float(v)
        return x if x == x and abs(x) < 1e15 else None
    except (ValueError, TypeError):
        return None


def clamp_percent(value):
    v = number(value)
    return None if v is None else round(max(0, min(100, v)), 2)


def window(percent, reset=None, label=""):
    parsed = parse_dt(reset)
    return {"used_percent": clamp_percent(percent), "resets_at": iso(parsed) if parsed else None, "label": label}


def load_json(path):
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def get_json(url, token, *, headers=None, timeout=8):
    h = {"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "orbit-usage/0.1"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError("Usage server returned an error")
            raw = resp.read(500_000)
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise RuntimeError("Unsupported usage response")
        return result
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("Usage endpoint rate-limited (HTTP 429)") from None
        raise RuntimeError(f"Usage endpoint returned HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("Usage endpoint unreachable") from None
    except TimeoutError:
        raise RuntimeError("Usage request timed out") from None


def _claude_token(keychain=False):
    """Read Claude Code OAuth access token from file and/or macOS Keychain."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    cred = load_json(config / ".credentials.json") or {}
    token = (cred.get("claudeAiOauth") or {}).get("accessToken")
    if token:
        return token
    if not (keychain and sys.platform == "darwin"):
        return None
    # Keychain reads can briefly fail under concurrent access; retry a few times.
    for attempt in range(3):
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=7,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return (json.loads(proc.stdout).get("claudeAiOauth") or {}).get("accessToken")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.15 * (attempt + 1))
    return None


def claude(keychain=False):
    token = _claude_token(keychain=keychain)
    if not token:
        hint = (
            "Try --keychain (macOS permission may appear)."
            if sys.platform == "darwin" and not keychain
            else "Sign in using claude."
        )
        raise RuntimeError("Claude Code OAuth credentials not readable. " + hint)

    # The undocumented usage endpoint rate-limits aggressively; retry briefly so
    # watch mode does not flap between real values and "n/a".
    data = None
    last_err = None
    for attempt in range(3):
        try:
            data = get_json(
                "https://api.anthropic.com/api/oauth/usage",
                token,
                headers={"anthropic-beta": "oauth-2025-04-20"},
            )
            break
        except RuntimeError as exc:
            last_err = exc
            msg = str(exc).lower()
            if "429" in msg or "timed out" in msg or "unreachable" in msg:
                time.sleep(0.7 * (attempt + 1))
                continue
            raise
    if data is None:
        raise last_err  # type: ignore[misc]

    def parse(item):
        if not isinstance(item, dict):
            return None
        pct = number(item.get("utilization", item.get("used_percent")))
        return window(pct, item.get("resets_at", item.get("reset_at"))) if pct is not None else None

    short, week = parse(data.get("five_hour")), parse(data.get("seven_day"))
    if not (short or week):
        raise RuntimeError("Claude response did not contain usage windows")
    return {
        "status": "ok",
        "source": "Claude Code OAuth (undocumented usage endpoint)",
        "five_hour": short,
        "weekly": week,
        "extras": {
            "opus_weekly": parse(data.get("seven_day_opus")),
            "sonnet_weekly": parse(data.get("seven_day_sonnet")),
        },
    }


_TRANSIENT_ERR = ("429", "rate-limited", "timed out", "unreachable")


def coalesce_transient(previous, live):
    """Keep the last good provider reading when a live poll hits a transient error.

    Credentials are usually fine; Claude's usage API in particular returns HTTP 429
    under watch polling. Prefer a slightly stale value over a flickering "n/a".
    """
    if not previous or not isinstance(previous.get("providers"), dict):
        return live
    merged = dict(live)
    merged["providers"] = dict(live.get("providers") or {})
    for name, cur in list(merged["providers"].items()):
        if not isinstance(cur, dict) or cur.get("status") != "unavailable":
            continue
        old = previous["providers"].get(name)
        if not isinstance(old, dict) or old.get("status") not in ("ok", "stale"):
            continue
        err = (cur.get("error") or "").lower()
        if not any(token in err for token in _TRANSIENT_ERR):
            continue
        kept = dict(old)
        kept["status"] = "stale"
        note = (kept.get("note") or "").strip("; ")
        kept["note"] = (note + "; last good reading after transient error").strip("; ")
        merged["providers"][name] = kept
    return merged


def codex_windows(data, stamp=None):
    """Normalize endpoint and rollout variants, honoring explicit window duration."""
    output = {"five_hour": None, "weekly": None}
    rate = data.get("rate_limit") or data.get("rate_limits") or data
    if not isinstance(rate, dict):
        return output
    fields = [
        ("five_hour", "five_hour"),
        ("weekly", "weekly"),
        ("primary_window", "five_hour"),
        ("secondary_window", "weekly"),
        ("primary", "five_hour"),
        ("secondary", "weekly"),
    ]
    for field, fallback in fields:
        v = rate.get(field)
        if not isinstance(v, dict):
            continue
        duration = number(v.get("limit_window_seconds"))
        target = ("weekly" if duration >= 172800 else "five_hour") if duration else fallback
        used = number(v.get("used_percent", v.get("usedPercent")))
        left = number(v.get("percent_left"))
        if used is None and left is not None:
            used = 100 - left
        if used is None:
            continue
        reset = v.get("reset_time_ms") or v.get("resetsAt") or v.get("resets_at")
        after = number(v.get("reset_after_seconds", v.get("resets_in_seconds")))
        if reset is None and after is not None:
            base = parse_dt(stamp) or dt.datetime.now(UTC)
            reset = iso(base + dt.timedelta(seconds=after))
        output[target] = window(used, reset, (f"{duration / 3600:g}-hour cap" if duration else ""))
    return output


def codex_from_rollouts(root):
    folder = root / "sessions"
    dates = [(dt.datetime.now(UTC) - dt.timedelta(days=i)).date() for i in range(9)]
    candidates = []
    for date in dates:
        day = folder / str(date.year) / f"{date.month:02}" / f"{date.day:02}"
        try:
            files = list(day.glob("rollout-*.jsonl"))[:120]
        except OSError:
            continue
        for path in files:
            try:
                if path.is_file():
                    candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for modified, path in sorted(candidates, reverse=True)[:40]:
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                start = max(0, size - 512 * 1024)
                f.seek(start)
                content = f.read().decode("utf-8", errors="replace")
                lines = content.splitlines()
                if start:
                    lines = lines[1:]
            for line in reversed(lines):
                if "rate_limits" not in line:
                    continue
                try:
                    item = json.loads(line)
                    rl = (item.get("payload") or {}).get("rate_limits") or item.get("rate_limits")
                    if not isinstance(rl, dict):
                        continue
                    timestamp = item.get("timestamp") or iso(dt.datetime.fromtimestamp(modified, tz=UTC))
                    windows = codex_windows({"rate_limits": rl}, timestamp)
                    if windows["five_hour"] or windows["weekly"]:
                        updated = parse_dt(timestamp)
                        stale = updated is None or (dt.datetime.now(UTC) - updated).total_seconds() > 1200
                        return {
                            "status": "stale" if stale else "ok",
                            "source": "Local Codex session snapshot",
                            "updated_at": timestamp,
                            **windows,
                            "note": "Historical snapshot; not an actively polled rate limit",
                        }
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError:
            continue
    raise RuntimeError("No Codex rate-limit reading. Sign in with codex login and use Codex.")


def codex():
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    auth = load_json(root / "auth.json") or {}
    tokens = auth.get("tokens") or {}
    token = tokens.get("access_token") or auth.get("access_token")
    reason = None
    if token:
        headers = {}
        account = tokens.get("account_id") or tokens.get("chatgpt_account_id") or auth.get("chatgpt_account_id")
        if account:
            headers["ChatGPT-Account-ID"] = str(account)
        try:
            data = get_json("https://chatgpt.com/backend-api/wham/usage", token, headers=headers)
            windows = codex_windows(data)
            if windows["five_hour"] or windows["weekly"]:
                return {"status": "ok", "source": "Codex OAuth (undocumented usage endpoint)", **windows}
            reason = "Unexpected Codex quota shape"
        except RuntimeError as e:
            reason = str(e)
    try:
        result = codex_from_rollouts(root)
        if reason:
            result["note"] = (result.get("note", "") + "; live endpoint unavailable").strip("; ")
        return result
    except RuntimeError as e:
        raise RuntimeError(f"{e}" + (f"; live: {reason}" if reason else "")) from None


def antigravity():
    from antigravity_probe import get_usage

    result = get_usage()
    models = []
    for m in result.get("models", []):
        name, remain = m.get("name"), number(m.get("remaining"))
        if name and remain is not None and 0 <= remain <= 1:
            models.append(
                {
                    "model": name,
                    "used_percent": round((1 - remain) * 100, 2),
                    "resets_at": iso(parse_dt(m.get("resetAt"))) if parse_dt(m.get("resetAt")) else None,
                }
            )
    if not models:
        raise RuntimeError("Antigravity did not expose measured quota windows")
    return {
        "status": "ok",
        "source": result.get("source", "Antigravity quota source"),
        "models": models,
        "note": (
            "Separate 5-hour and weekly Gemini/Claude-GPT quota pools"
            if result.get("grouped")
            else "IDE model quotas; 5-hour and weekly limits may not be available"
        ),
    }


def kiro():
    from kiro_usage import get_kiro_usage

    result = get_kiro_usage()
    q = result["quota"]
    activity = result["activity"]
    used, limit = number(q.get("used")), number(q.get("limit"))
    if used is None or limit is None or limit <= 0:
        raise RuntimeError("Kiro subscription credit count unavailable")
    return {
        "status": "ok",
        "source": "Kiro CLI ACP usage method",
        "monthly": {
            "used_credits": used,
            "limit_credits": limit,
            "used_percent": clamp_percent(100 * used / limit),
            "resets_at": q.get("resetAt"),
        },
        "five_hour_activity_credits": activity.get("shortCredits") if activity.get("available") else None,
        "weekly_activity_credits": activity.get("weekCredits") if activity.get("available") else None,
        "note": "5-hour and weekly numbers are local activity, NOT subscription quotas.",
    }


def cursor():
    from cursor_probe import probe

    return probe()


COLLECTORS = {"claude": claude, "codex": codex, "antigravity": antigravity, "kiro": kiro, "cursor": cursor}


def snapshot(selected=PROVIDERS, *, keychain=False):
    output = {"checked_at": iso(), "providers": {}}

    # Collect concurrently so a slow desktop integration doesn't hold up the others.
    # Each collector has its own process and/or network timeouts.
    def check(name):
        try:
            return COLLECTORS[name](keychain=keychain) if name == "claude" else COLLECTORS[name]()
        except Exception as e:
            # Never print tracebacks, HTTP payloads, identities, or authentication tokens.
            return {"status": "unavailable", "error": str(e)[:210]}

    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        jobs = {name: pool.submit(check, name) for name in selected}
        for name in selected:
            output["providers"][name] = jobs[name].result()
    return output


def compact(snap):
    terms = []
    for name, d in snap["providers"].items():
        if d.get("status") == "unavailable":
            terms.append(name + " n/a")
        elif name in ("claude", "codex"):
            a = d.get("five_hour") or {}
            b = d.get("weekly") or {}
            fmt = lambda x: f"{x:.0f}%" if x is not None else "n/a"
            mark = "~" if d.get("status") == "stale" else ""
            terms.append(f'{name}{mark} 5h: {fmt(a.get("used_percent"))}, 7d: {fmt(b.get("used_percent"))}')
        elif name == "kiro":
            x = d["monthly"]
            terms.append(f'kiro {x["used_credits"]:g}/{x["limit_credits"]:g} cr')
        elif name == "antigravity":
            # Show each model family (Google/Gemini and Others) with its 5-hour
            # and 7-day usage, so neither pool nor the 5h window is hidden.
            short = {"Gemini Models": "Google", "Claude and GPT models": "Others"}
            families = {}  # family -> {'5h': pct, '7d': pct}
            for m in d["models"]:
                base, _, cadence = m["model"].partition(" · ")
                label = short.get(base.strip(), base.strip()[:10])
                win = "5h" if "5" in cadence else ("7d" if "7" in cadence or "day" in cadence else cadence.strip())
                families.setdefault(label, {})[win] = m["used_percent"]
            parts = []
            for label, wins in families.items():
                seg = " ".join(f"{w}: {wins[w]:.0f}%" for w in ("5h", "7d") if w in wins)
                extra = " ".join(f"{w}: {p:.0f}%" for w, p in wins.items() if w not in ("5h", "7d"))
                parts.append(f"{label} {seg} {extra}".strip())
            terms.append("AGY " + " ; ".join(parts))
        elif name == "cursor":
            # Lead with Auto/models % (Cursor UI "usage"), then included total
            # (totalPercentUsed), matching the dashboard wording.
            parts = []
            if d.get("cursor_models_percent") is not None:
                parts.append(f'{d["cursor_models_percent"]:.0f}%')
            if d.get("included_used_percent") is not None:
                parts.append(f'{d["included_used_percent"]:.0f}% included')
            other = d.get("other_models_percent")
            if other is not None and other > 0:
                parts.append(f"{other:.0f}% other")
            terms.append("cursor " + (" ".join(parts) if parts else "quota n/a"))
    return " | ".join(terms)


def countdown(value):
    target = parse_dt(value)
    if not target:
        return ""
    delta = int((target - dt.datetime.now(UTC)).total_seconds())
    if delta <= 0:
        return "reset due"
    d, r = divmod(delta, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return "resets in " + ((f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m")


def bar(pct, width=16):
    if pct is None:
        return "·" * width + "   n/a"
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled) + f" {pct:5.1f}%"


def line_window(name, win):
    if win is None:
        return f"    {name:<12} {bar(None)}"
    return f'    {name:<12} {bar(win.get("used_percent"))}  {countdown(win.get("resets_at"))}'.rstrip()


def render(snap):
    lines = [f'ORBIT USAGE  {snap["checked_at"]}', "─" * 66]
    names = {
        "claude": "Claude Code",
        "codex": "Codex",
        "antigravity": "Antigravity",
        "kiro": "Kiro CLI",
        "cursor": "Cursor CLI",
    }
    for provider, data in snap["providers"].items():
        status = data.get("status", "unavailable")
        lines.append(f"\n{names[provider]}  [{status}]")
        if status == "unavailable":
            lines.append("    " + data.get("error", data.get("note", "Usage unavailable")))
            continue
        if provider in ("claude", "codex"):
            lines.append(line_window("5 hours", data.get("five_hour")))
            lines.append(line_window("7 days", data.get("weekly")))
        elif provider == "antigravity":
            if data.get("source"):
                lines.append("    Source        " + data["source"])
            for model in data.get("models", []):
                lines.append("    " + model["model"][:38])
                lines.append("      " + bar(model.get("used_percent")) + "  " + countdown(model.get("resets_at")))
        elif provider == "kiro":
            q = data["monthly"]
            lines.append("    Monthly       " + bar(q["used_percent"]) + "  " + countdown(q.get("resets_at")))
            lines.append(f'    Credits       {q["used_credits"]:g} / {q["limit_credits"]:g}')
            v5, v7 = data.get("five_hour_activity_credits"), data.get("weekly_activity_credits")
            if v5 is not None or v7 is not None:
                lines.append(
                    f'    Local activity  5h: {v5 if v5 is not None else "n/a"} credits  |  7d: {v7 if v7 is not None else "n/a"} credits'
                )
        elif provider == "cursor":
            lines.append("    Session       " + data.get("session_source", "Unknown"))
            if data.get("plan"):
                lines.append("    Plan          " + data["plan"])
            if data.get("quota_type") == "billing_cycle":
                lines.append(
                    line_window(
                        "Billing cycle",
                        {"used_percent": data.get("included_used_percent"), "resets_at": data.get("resets_at")},
                    )
                )
            elif data.get("included_used_percent") is not None:
                lines.append(line_window("Plan used", {"used_percent": data["included_used_percent"]}))
            if data.get("cursor_models_percent") is not None:
                lines.append("    Cursor models " + bar(data["cursor_models_percent"]))
            if data.get("other_models_percent") is not None:
                lines.append("    Other models  " + bar(data["other_models_percent"]))
            if data.get("included_used_units") is not None and data.get("included_limit_units") is not None:
                # Dashboard returns these in internal plan units, typically cents.
                lines.append(
                    f'    Allowance     {data["included_used_units"]:g} / {data["included_limit_units"]:g} plan units'
                )
            if data.get("note"):
                lines.append("    " + data["note"])
        if data.get("status") == "stale":
            lines.append("    ⚠ Historical snapshot; values may have changed")
        elif data.get("note") and provider in ("antigravity", "kiro"):
            lines.append("    " + data["note"])
    lines.append("\nRead-only. No inference calls. Cursor usage comes from signed-in dashboard; unofficial API.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Display Claude, Codex, Antigravity, Kiro and Cursor usage using existing CLI sessions"
    )
    parser.add_argument("--only", metavar="NAMES", help="Comma-separated providers: " + ",".join(PROVIDERS))
    parser.add_argument(
        "--watch", type=int, metavar="SECONDS", help="Refresh in terminal every N seconds (minimum 30)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Print credential-free JSON; watch mode emits one JSON line per refresh"
    )
    parser.add_argument(
        "--compact", action="store_true", default=True, help="Print a single line, suitable for terminal or tmux"
    )
    parser.add_argument(
        "--keychain",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow reading existing Claude Code OAuth from macOS Keychain (on by default; use --no-keychain to disable)",
    )
    parser.add_argument(
        "--diagnose-agy", action="store_true", help="Show safe Antigravity detection checks (no quota requests)"
    )
    parser.add_argument(
        "--diagnose-cursor", action="store_true", help="Show safe local Cursor session checks (no quota requests)"
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Reuse a cached snapshot if it is younger than N seconds "
        "(default 30; 0 = always live). "
        "Cache is a single temp file overwritten each refresh.",
    )
    args = parser.parse_args(argv)
    if args.diagnose_cursor:
        from cursor_probe import diagnostics

        result = diagnostics()
        print(json.dumps(result, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in result.items()))
        return 0
    if args.diagnose_agy:
        from antigravity_probe import diagnostics

        result = diagnostics()
        print(json.dumps(result, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in result.items()))
        return 0
    if args.json and args.compact:
        parser.error("--json and --compact cannot be used together")
    selected = (
        PROVIDERS
        if args.only is None
        else tuple(dict.fromkeys(p.strip().lower() for p in args.only.split(",") if p.strip()))
    )
    if not selected or any(x not in PROVIDERS for x in selected):
        parser.error("Unknown provider(s). Use: " + ",".join(PROVIDERS))
    if args.watch is not None and args.watch < 30:
        parser.error("--watch minimum is 30 seconds; 300 is recommended")
    last_good = None
    try:
        while True:
            result = read_cache(selected, args.max_age)
            if result is None:
                live = snapshot(selected, keychain=args.keychain)
                # Fall back to in-memory or on-disk last-good when a provider
                # flaps on transient errors (especially Claude HTTP 429).
                prior = last_good or read_cache(selected, max_age=7 * 24 * 3600)
                result = coalesce_transient(prior, live)
                write_cache(selected, result)
            last_good = result
            if args.json:
                print(json.dumps(result, separators=(",", ":"), ensure_ascii=False), flush=True)
            elif args.compact:
                if args.watch and sys.stdout.isatty():
                    # Overwrite the single status line in place instead of scrolling.
                    print("\r\x1b[2K" + compact(result), end="", flush=True)
                else:
                    print(compact(result), flush=True)
            else:
                if args.watch and sys.stdout.isatty():
                    print("\x1b[2J\x1b[H", end="")
                print(render(result), flush=True)
            if args.watch is None:
                break
            time.sleep(args.watch)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
