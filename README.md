# Kairos

> *Kairos* (καιρός) — the Greek god of the opportune moment. A Minecraft Java
> Edition username sniper that seizes a name the instant it drops.

Kairos authenticates **your own** Microsoft/Minecraft account, watches a target
username, and fires a burst of concurrent claim requests at the exact moment it
becomes available. Use it from the command line or through a modern web UI.

---

## Features

- **Full Microsoft auth chain** — device-code OAuth2 (browser login, no password)
  → Xbox Live → XSTS → Minecraft bearer token.
- **Token caching** — refresh token saved to disk, so re-runs skip the login.
- **Auto-refresh** — the bearer token is refreshed before expiry during long waits.
- **NTP clock sync** — corrects for system clock drift so timing lines up with the
  real drop, not your machine's skewed clock.
- **Cooldown guard** — verifies the account has no active name-change cooldown
  before sniping.
- **Tuned sniping** — polls availability every 500 ms from T-3 min, then fires N
  concurrent `PUT` claims at T-2 s, stopping on the first HTTP 200.
- **Graceful 429 handling** — logs rate limits and keeps going.
- **Web UI** — Kairos-themed dashboard with a live countdown and streamed log
  (Server-Sent Events).

---

## Project structure

| File | Role |
|------|------|
| `auth.py` | Microsoft → Xbox → XSTS → Minecraft auth chain + token cache |
| `api.py` | Availability polling and the concurrent claim burst |
| `sniper.py` | CLI entry point: NTP sync, drop-time resolution, orchestration |
| `webapp.py` | FastAPI server (REST + SSE) serving the web UI |
| `static/index.html` | Web frontend (self-contained, no build step) |
| `config.json` | User-editable settings (used by the CLI) |
| `Dockerfile`, `DEPLOY.md` | Container + free-tier cloud deployment |

---

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## Usage

### Web UI (recommended)

```bash
.venv/bin/python -m uvicorn webapp:app --port 8000
```

Open <http://localhost:8000>:

1. **Connect Microsoft account** — sign in once (device code). The token is cached.
2. **Target** — enter the username and drop time (ISO 8601, UTC). Use **Check on
   NameMC** if you're unsure of the exact name.
3. **Run sniper** — watch the live countdown and log; a green pill confirms the claim.

Add `--reload` while editing the frontend.

### CLI

Edit `config.json`, then:

```bash
.venv/bin/python sniper.py
```

Test auth in isolation:

```bash
.venv/bin/python auth.py
```

---

## Configuration (`config.json`)

```json
{
  "target_name": "",
  "drop_time": "",
  "burst_start_seconds": 2,
  "concurrent_requests": 20,
  "poll_interval_ms": 500,
  "client_id": "",
  "ntp_server": "pool.ntp.org"
}
```

- `target_name` — username to claim (required).
- `drop_time` — ISO 8601, assumed UTC if no timezone given. **Required** (see below).
- `burst_start_seconds` — fire the claim burst this many seconds before the drop.
- `concurrent_requests` — number of simultaneous claim requests.
- `poll_interval_ms` — availability poll cadence.
- `client_id` — leave blank to use the bundled public MSA app, or set your own
  Azure app id (with device-code flow and the `XboxLive.signin` scope).
- `ntp_server` — NTP server used for clock sync.

---

## Deployment

See [`DEPLOY.md`](DEPLOY.md) — Oracle Cloud Always Free (recommended), GCP
e2-micro, Cloud Run, or a `systemd` service.

---

## Notes & limitations

- **Drop time must be set manually.** Auto-detect (last name change + 37 days)
  is implemented but non-functional: Mojang removed the public name-history API
  in 2022, and fallback sources no longer return change dates. Set `drop_time`
  yourself.
- **Run against your own account only.** Kairos uses your authenticated session
  to claim names on your profile.
- **Don't expose the web UI publicly.** It drives your account with no built-in
  auth. On a cloud VM, bind to `127.0.0.1` and reach it over an SSH tunnel
  (details in `DEPLOY.md`).
- Name sniping may conflict with Mojang/Microsoft terms of service. Use at your
  own risk.

---

## Requirements

Python 3.10+ · `aiohttp` · `ntplib` · `fastapi` · `uvicorn`
