<div align="center">

# Seizr

### Seize the moment a name drops.

**Seizr watches the Minecraft username you want and claims it the instant it
opens. Synced to real time, so you don't have to sit and watch the clock.**

[Quick start](#quick-start) · [How it works](#how-it-works) · [Trust & safety](#trust--safety) · [Deploy](#deploy-your-own) · [Docs](docs/TECHNICAL.md)

</div>

---

> Named for *Kairos* (καιρός), the Greek god of the fleeting, opportune moment —
> he wears a long forelock you grab as he rushes past; hesitate and he's gone.
> **Seizr** is that grab.

## Why Seizr

- ⏱️ **Perfect timing** — your clock is synced to an NTP server, so Seizr fires
  on the *real* drop time, not your machine's drift.
- 🎯 **Wins the race** — at the drop it sends a burst of simultaneous claims and
  stops the instant one lands the name.
- 🛟 **Safe by default** — sensible request rates and automatic back-off keep your
  account clear of rate-limit trouble.
- 🔒 **Your account, your control** — sign in once with Microsoft, no password ever
  stored. Seizr only ever acts on your own profile.
- 🪶 **Set it and walk away** — give it the name and its window; it watches the
  whole window and seizes the moment for you.

## How it works

1. **Connect** your Microsoft account — once, in your own browser. No password stored.
2. **Paste** the name and its drop window (read off [NameMC](https://namemc.com)).
3. **Seizr watches** the window and claims the name the instant it opens.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn webapp:app --port 8000
```

Open **http://localhost:8000**, connect your account, enter the name + window, and
let Seizr watch. Prefer the terminal? There's a CLI too — see the
[technical docs](docs/TECHNICAL.md).

## Trust & safety

Seizr handles your Microsoft account, so it is **fully open source** — read every
line that touches your credentials before you run it.

- **Your password is never seen or stored.** Login uses Microsoft's OAuth2
  device-code flow — you authenticate on `microsoft.com` yourself.
- **Only a refresh token is kept**, owner-only on disk or on a volume you control.
  No password, email, or payment data.
- **Two hosts only** — Microsoft login and the Minecraft API. No telemetry, no
  third parties.

Details and how to report an issue: [`SECURITY.md`](SECURITY.md).

## Deploy your own

Runs on a free always-on VM. See [`DEPLOY.md`](DEPLOY.md) — Oracle Cloud Always
Free (recommended), GCP, Cloud Run, or `systemd`. Host in **US East / Ashburn**
for the lowest latency to Mojang's API.

> ⚠️ The web UI drives your account and has no built-in auth — don't expose it to
> the public internet. Bind to localhost and reach it over an SSH tunnel.

## Documentation

Architecture, configuration reference, CLI usage, and limitations live in
**[docs/TECHNICAL.md](docs/TECHNICAL.md)**.

## License

**GNU Affero General Public License v3.0** ([`LICENSE`](LICENSE)). Use, study,
self-host, and fork freely — but anyone who runs a modified version as a service
must publish their changes. Every deployment of Seizr stays open.

"Seizr" and its logo are project marks — forks must use a different name.
