# Deploying Seizr (free tier)

Seizr is a single FastAPI server (`webapp:app`) serving the UI + API. It keeps
one auth token cache on disk. Best fit for a small always-on VM — **Oracle Cloud
Always Free** or a **GCP e2-micro**. Cloud Run works too but resets the token
cache on cold start (you re-login), so a VM is preferred for a personal sniper.

> Security: this app drives **your own** Microsoft/Minecraft account. Do not
> expose it to the public internet without auth in front. Bind it to localhost
> and reach it over an SSH tunnel, or put a password (reverse proxy / basic auth)
> in front, or restrict the firewall to your IP.

---

## Option A — Oracle Cloud Always Free (recommended)

Always Free gives you an Ampere ARM VM (up to 4 vCPU / 24 GB) that never expires.

1. Create an **Always Free** VM instance (Ubuntu 22.04, ARM/Ampere shape).
2. Open port 8000 only to your IP (VCN security list / `iptables`), or skip this
   and use the SSH tunnel below instead (safer).
3. On the VM:
   ```bash
   sudo apt update && sudo apt install -y docker.io git
   git clone <your-repo> seizr && cd seizr
   sudo docker build -t seizr .
   sudo docker run -d --name seizr --restart unless-stopped \
     -p 127.0.0.1:8000:8000 -v seizr-data:/data seizr
   ```
   `-p 127.0.0.1:8000:8000` keeps it private. The `seizr-data` volume persists
   the login token across restarts.
4. Reach it from your laptop over SSH tunnel:
   ```bash
   ssh -L 8000:localhost:8000 ubuntu@<vm-ip>
   # then open http://localhost:8000
   ```

## Option B — GCP e2-micro free tier

Same as Oracle but x86. `e2-micro` in a free-tier region is Always Free.
Use the identical Docker commands. Tunnel with:
```bash
gcloud compute ssh seizr-vm -- -L 8000:localhost:8000
```

## Option C — GCP Cloud Run (stateless)

Quick but the token cache is ephemeral (re-login after each cold start):
```bash
gcloud run deploy seizr --source . --port 8000 --allow-unauthenticated=false --region <region>
```
Use `--no-allow-unauthenticated` and access via `gcloud run services proxy`.

---

## Run without Docker (any VM)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn webapp:app --host 0.0.0.0 --port 8000
```
For always-on, wrap it in a `systemd` unit with `Restart=always`.

## Local dev

```bash
.venv/bin/python -m uvicorn webapp:app --port 8000   # http://localhost:8000
```

## Environment

Copy `.env.example` to `.env` and fill it in. Load it with `docker run --env-file
.env`, a systemd `EnvironmentFile=`, or `set -a; source .env; set +a` locally.
Set at least `SEIZR_SECRET_KEY` (encrypts stored Minecraft tokens) and
`SECURE_COOKIES=1` behind HTTPS.

## Email (Resend)

Account verification emails send via SMTP. With no `SMTP_HOST` set, Seizr prints
the verification link to the server log (dev mode). To send real email with Resend:

1. Sign up at [resend.com](https://resend.com), create an **API key**
   (Settings → API Keys).
2. **Verify your sending domain** (Domains → add `seizr.io`, then add the shown
   SPF + DKIM records at your DNS registrar). Until verified, you can only send
   from `onboarding@resend.dev` to your own account email — fine for testing.
3. Set the env vars:
   ```
   SMTP_HOST=smtp.resend.com
   SMTP_PORT=587
   SMTP_STARTTLS=1
   SMTP_USER=resend
   SMTP_PASS=re_your_api_key
   SMTP_FROM=no-reply@seizr.io      # must be on the verified domain
   ```
Seizr uses plain SMTP, so no code changes are needed — just the env vars.

## NTP note
The sniper syncs the clock against `pool.ntp.org` (UDP 123) at run start. Make
sure outbound UDP 123 is allowed, or accuracy falls back to the system clock.
