# Seizr — Technical Reference

Architecture, configuration, CLI usage, and limitations. For the product overview
and quick start, see the [README](../README.md).

## Architecture

| File | Role |
|------|------|
| `auth.py` | Microsoft → Xbox Live → XSTS → Minecraft auth chain + refresh-token cache |
| `api.py` | Availability polling, adaptive 429 backoff, the concurrent claim burst |
| `sniper.py` | CLI entry point: NTP sync, window resolution, orchestration |
| `webapp.py` | FastAPI server (REST + Server-Sent Events) serving the web UI |
| `static/` | Frontend — `landing.html`, `app.html`, shared `style.css`, `favicon.svg` |
| `config.json` | User-editable settings (used by the CLI) |
| `Dockerfile`, `DEPLOY.md` | Container + free-tier cloud deployment |

### Auth chain (`auth.py`)

OAuth2 **device-code flow** (no password) → Xbox Live → XSTS → Minecraft bearer
token. The Microsoft *refresh token* is cached (`.auth_cache.json`, `chmod 600`,
or the `AUTH_CACHE` path on a server volume) so re-runs skip the browser step. The
bearer token auto-refreshes before expiry during long waits.

### Sniping model (`api.py`)

Mojang no longer drops names at an exact instant — a name frees up somewhere
inside a **window**. `snipe_window(start, end, poll_ms)`:

1. Waits for the window to open.
2. Polls availability across the whole window.
3. On the first `AVAILABLE` flip, fires up to `AGGRESSIVE_ROUNDS` (6) back-to-back
   bursts of `concurrent_requests` simultaneous `PUT` claims, stopping on HTTP 200.
4. Backs off on HTTP 429 (interval doubles, capped at 10 s) and recovers on the
   next clean response — staying under the rate limit so it remains responsive.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

### Web UI

```bash
.venv/bin/python -m uvicorn webapp:app --port 8000   # http://localhost:8000
```

Add `--reload` while editing the frontend.

### CLI

Edit `config.json`, then:

```bash
.venv/bin/python sniper.py
```

Test the auth chain in isolation:

```bash
.venv/bin/python auth.py
```

## Configuration (`config.json`)

```json
{
  "target_name": "",
  "window_start": "",
  "window_end": "",
  "concurrent_requests": 5,
  "poll_interval_ms": 1000,
  "client_id": "",
  "ntp_server": "pool.ntp.org"
}
```

- `target_name` — username to claim (required).
- `window_start` — ISO 8601 (UTC if no tz). When the drop window opens. **Required.**
- `window_end` — ISO 8601. When the window closes; blank defaults to start + 15 min.
- `concurrent_requests` — simultaneous claim requests per burst. Default **5**:
  enough to hedge network jitter and win the race, low enough to avoid the 429
  flood that can temporarily suspend an account. Raise cautiously.
- `poll_interval_ms` — availability poll cadence. Default **1000** (1/sec) matches
  Mojang's ~600-requests-per-10-minutes ceiling, so steady polling stays under the
  limit. Polling auto-backs-off on 429 and recovers. Lower it only for short windows.
- `client_id` — leave blank to use the bundled public MSA app, or set your own
  Azure app id (with device-code flow and the `XboxLive.signin` scope).
- `ntp_server` — NTP server used for clock sync.

> The web UI takes the same inputs through pickers; the drop window is entered in
> your local time and converted to UTC automatically.

## Deployment

See [`DEPLOY.md`](../DEPLOY.md) — Oracle Cloud Always Free (recommended), GCP
e2-micro, Cloud Run, or a `systemd` service. For lowest latency to Mojang's API
(fronted by Azure Front Door), host in **US East / Ashburn**.

## Notes & limitations

- **Drops happen in a window, not an instant.** Read the window off NameMC and set
  the times manually — there is no public API for it (NameMC offers none, and
  Mojang's name-history API was removed in 2022).
- **Long windows risk rate limits.** Widen `poll_interval_ms` for multi-hour
  windows; the adaptive backoff helps but can't make a tight poll free.
- **Run against your own account only.** Seizr uses your authenticated session to
  claim names on your own profile.
- **Don't expose the web UI publicly.** It drives your account with no built-in
  auth. Bind to `127.0.0.1` and reach it over an SSH tunnel (see `DEPLOY.md`).
- Name sniping may conflict with Mojang/Microsoft terms of service. Use at your
  own risk.

## Requirements

Python 3.10+ · `aiohttp` · `ntplib` · `fastapi` · `uvicorn`
