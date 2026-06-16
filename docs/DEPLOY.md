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

> **Latency is the whole game.** A name drop is a race won on round-trip time to
> Mojang's API. `api.minecraftservices.com` is served by **Azure Front Door**
> (Microsoft's anycast edge), so you want a VM in a major US-East peering metro —
> **Ashburn, Virginia**. From a home machine the API is ~350 ms away; from
> Ashburn you should see well under 50 ms.

### ⚠️ The one permanent decision: home region

Oracle locks all **Always Free** resources to the **home region you pick at
signup — and you can never change it.** Choose **`US East (Ashburn)`**
(`us-ashburn-1`). Pick the wrong region and you're stuck with worse latency
forever. This single choice matters more than any code tuning.

### Shape: use the AMD micro, not the ARM Ampere

- **Pick `VM.Standard.E2.1.Micro`** (AMD, 1/8 OCPU, 1 GB). Sniping is
  network-bound, not CPU-bound — this is plenty.
- **Avoid the ARM Ampere A1** shape: it's usually *"out of capacity"* in Ashburn,
  and Oracle **reclaims idle Always-Free A1 instances** — bad for a box that sits
  quiet between drops and must be up at the exact moment.
- OS: **Ubuntu 22.04**.

### Steps

1. Create the instance (Ubuntu 22.04, `E2.1.Micro`) in your Ashburn home region.
2. **Verify latency first** — SSH in and run:
   ```bash
   curl -s -o /dev/null -w "tls=%{time_appconnect}s ttfb=%{time_starttransfer}s\n" \
     https://api.minecraftservices.com/minecraft/profile/name/Notch/available
   ```
   Expect `ttfb` well under ~0.05 s. If it's high, the region is wrong.
3. Build and run (Docker; the image is core-only — no Chromium):
   ```bash
   sudo apt update && sudo apt install -y docker.io git
   git clone <your-repo> seizr && cd seizr
   sudo docker build -t seizr .
   sudo docker run -d --name seizr --restart unless-stopped \
     -p 127.0.0.1:8000:8000 -v seizr-data:/data \
     -e SEIZR_SECRET_KEY="$(openssl rand -base64 48)" \
     seizr
   ```
   - `-p 127.0.0.1:8000:8000` keeps it private (reach it via the tunnel below).
   - `-v seizr-data:/data` persists the SQLite DB + login token across restarts.
   - **`SEIZR_SECRET_KEY` encrypts stored Minecraft tokens.** Set it once and keep
     it stable — if it changes, every saved login is invalidated and you re-auth.
     Save the generated value (e.g. in an `.env`) so a rebuild reuses it.
4. Reach the UI from your laptop over an SSH tunnel:
   ```bash
   ssh -L 8000:localhost:8000 ubuntu@<vm-ip>
   # then open http://localhost:8000
   ```

### The drop window: enter it manually

The NameMC autofill is **not** installed on the VM — NameMC's Cloudflare blocks
datacenter IPs, and a drop window is a **static value** anyway. So:

1. On your **home machine**, run Seizr locally and type the name — the window
   autofills (or read it off NameMC directly).
2. Copy the two timestamps and **enter them by hand** in the VM's UI (Window
   opens / Window closes). Done once per name.

### Keep it alive

Oracle may reclaim idle Always-Free compute. The `--restart unless-stopped`
container keeps Seizr running, which is enough activity. Don't stop the container
between drops.

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
