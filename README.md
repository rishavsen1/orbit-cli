# Orbit Usage CLI v0.3 — Cursor account usage support

A single read-only terminal command for Claude Code, Codex, **Antigravity (`agy`)**, Kiro CLI and **Cursor IDE/CLI**. Requires Python 3.10+, standard library only. No AI model prompts or analytics service.

## Install and upgrade from v0.2

Extract `orbit-usage-cli-v0.3-cursor-fix.zip` and use the included `orbit-cli` folder, or copy **both `cursor_probe.py` and `orbit_usage.py`** into the folder containing your previous `./orbit`. Other included files preserve the `agy` and Kiro fixes.

```bash
cd orbit-cli
./orbit --only cursor
./orbit --only cursor,claude,codex,antigravity,kiro
./orbit --watch 300
```

You **do not need to sign in again** if your existing Cursor CLI or Cursor IDE session is accessible locally:

1. `CURSOR_SESSION_TOKEN` environment variable (explicit override, intended for troubleshooting).
2. **macOS**: existing `cursor-agent` / `agent` sign-in token stored in Keychain (`cursor-access-token`). macOS may show a one-time permission prompt.
3. **macOS/Linux/Windows**: Cursor IDE's local read-only SQLite state DB (`cursorAuth/accessToken` in `User/globalStorage/state.vscdb`). No Cursor editor API or browser extension is required; you just need to have signed in to the Cursor app once.

Cursor CLI `agent status` only validates your login, not usage. The new collector does **not** treat that as quota. The collected dashboard statistics are account-wide, covering eligible Cursor IDE and CLI usage attributed to your signed-in account.

### Output

On plans with compatible quota fields, Cursor will appear as, for example:

```text
Cursor CLI  [ok]
    Session       Existing cursor-agent macOS Keychain session
    Plan          pro
    Billing cycle ████████░░░░░░░░  50.0%  resets in 13d 8h
    Cursor models ███░░░░░░░░░░░░░  18.0%
    Other models  ██████░░░░░░░░░░  36.0%
    Allowance     1000 / 2000 plan units
    Account-wide usage (IDE + CLI), not 5-hour usage. Cursor dashboard APIs are unofficial.
```

**Values above are illustrative.** The allowance in `plan units` is not labeled as dollars because usage/account plans and backend formats can vary. Cursor exposes some model percentages as distinct metrics that do not necessarily sum to the overall included allowance percentage.

The current **legacy Cursor plans have monthly usage pools**. Cursor's **The X Subscription** plans use a shared **weekly** allowance, shown at [grok.com/dashboard](https://grok.com/dashboard). This tool currently recognizes the legacy `cursor.com` dashboard schemas. If your X Subscription quota is not provided through those schemas, Orbit shows **unavailable** rather than inferring a limit or reset. Your account may be on a different plan; this tool does not assume otherwise. **No public personal-account usage API/CLI command is supported by Cursor.**

### If Cursor is still unavailable

```bash
./orbit --diagnose-cursor
./orbit --only cursor
```

Safe diagnostic mode only reports *whether* local sources exist, with **no network requests, tokens, account IDs, or passwords displayed**.

If you only have Cursor CLI on Linux/Windows, the current v0.3 may not know how to read its OS-specific credential store. Signing into the Cursor desktop app once provides a standard local state database that Orbit knows how to read. On macOS the Cursor CLI Keychain login alone should be enough if the local token is accessible.

Advanced fallback: use a temporary `CURSOR_SESSION_TOKEN` environment variable containing **your own** `WorkosCursorSessionToken` cookie copied from your signed-in `cursor.com` dashboard. This cookie grants access to your account: do **not** paste it into a chat, script, issue, terminal command history, or shared file; clear the environment variable afterward. Prefer the automatic credential sources.

### Security and compatibility

- Credentials are read into memory only, never written or printed by Orbit, and used **only** for requests to `https://cursor.com/api/...`; HTTP redirects are disabled to avoid forwarding cookies.
- The IDE SQLite file is opened `mode=ro`; the CLI does not edit or store account credentials, perform model inference, or start a browser login.
- For the Cursor dashboard, the script may send a GET request to `/api/usage-summary` and `/api/auth/me`, plus a read-only (JSON POST) usage query to `/api/dashboard/get-current-period-usage`.
- These are **undocumented Cursor endpoints** which may change, return stale values, or disappear. They are not an official Cursor integration. Account-wide reporting may include activity from other Cursor surfaces that share your subscription.
- Source: [Cursor community confirming no supported individual usage API](https://forum.cursor.com/t/usage-api-cli-command/160967), [public read-only implementation and protocol notes](https://github.com/javaisbetterthanpython/cursor-usage), [Cursor limits documentation](https://prod.cursor.com/help/models-and-usage/usage-limits).

## Other CLI commands

```bash
./orbit                   # Everything
./orbit --only cursor      # Cursor only
./orbit --only antigravity # agy CLI quota with running-IDE fallback
./orbit --diagnose-agy     # Antigravity safe diagnostics
./orbit --diagnose-cursor  # Cursor safe diagnostics
./orbit --json             # Credential-free JSON (suitable for scripts)
./orbit --compact          # One-line terminal/tmux status
./orbit --watch 300        # Refresh periodically, while terminal stays open
./orbit --max-age 60       # Reuse a cached snapshot younger than N seconds (default 30; 0 = always live)
./orbit --no-keychain      # Disable *Claude Code* Keychain access (on by default on macOS)
python3 -m unittest discover -s tests -v
```

Note: as of this build, `--keychain` is **on by default** on macOS. Use `--no-keychain` to opt out.

## Run `orbit` from anywhere (global command)

The `orbit` wrapper resolves its own symlinks, so you can symlink it into a directory
already on your `PATH` and call `orbit` from any folder:

```bash
# system-wide (needs sudo; /usr/local/bin is on PATH by default on macOS)
sudo ln -s "$(pwd)/orbit" /usr/local/bin/orbit

# or, no sudo required (ensure ~/.local/bin is on your PATH)
mkdir -p ~/.local/bin
ln -s "$(pwd)/orbit" ~/.local/bin/orbit
```

Then simply:

```bash
orbit --compact --max-age 60
```

## Performance and caching

A cold run that queries every provider takes a few seconds, and this is **not** a
Python limitation — it is dominated by work outside Orbit's own code:

- **Antigravity** and **Kiro** each spawn an external CLI subprocess (`agy`,
  `kiro-cli`) and wait for its startup, sign-in check and reply (~3–4s each).
- **Claude**, **Codex** and **Cursor** make network round-trips to provider APIs.

Because collectors run **concurrently**, total time is roughly the slowest single
provider, not the sum. Rewriting Orbit in Rust or C++ would **not** meaningfully
help: a compiled binary still pays the same external-process-launch and network
latency, so it would save only tens of milliseconds of interpreter overhead while
sacrificing the "standard library only, no install, readable" design. The effective
speed-up is to **avoid repeating that work**, which `--max-age` does:

```bash
./orbit --compact --max-age 60   # ~70 ms when the cache is < 60 s old, else live
```

The cache is a **single fixed temp file** (`<tempdir>/orbit-usage-cache.json`) that is
**overwritten in place** on every refresh via an atomic replace. It never accumulates
copies or grows unbounded, and it is keyed to the exact set of selected providers, so
`--only claude` will not serve a snapshot built for all providers. The default is
`--max-age 30` (reuse a snapshot younger than 30 s); pass `--max-age 0` to always
collect live data.

## Provider reporting

| Provider | Fields | Limitations |
|---|---|---|
| Claude Code | Five-hour and seven-day usage % and resets | Unofficial OAuth usage endpoint; Keychain opt-in on macOS |
| Codex | Five-hour and seven-day quota % and resets | Unofficial endpoint; may use stale local session snapshots |
| Antigravity | Five-hour and seven-day group quotas when reported; per-model quotas | `agy` CLI `/usage` read-only, running-IDE fallback; plan-dependent |
| Kiro CLI | Monthly subscription credits and reset; locally observed 5h/7d credit activity | Activity is **not** a rolling subscription quota |
| **Cursor** | Account-wide included-plan %, model pool %, billing reset when supported | No public individual API; newer X Subscription weekly quota may not be exposed through the legacy dashboard |

**Validation:** Parser/unit tests use artificial API results, a temporary Cursor SQLite DB, simulated credential resolution and mock HTTP calls. The collector has not been validated against your authenticated Cursor account; use the diagnostics above if your local configuration differs.
