# Security

Seizr drives **your own** Microsoft / Minecraft account. This document explains
exactly what it touches, what it stores, and how to report a problem. The code
backing every claim here is in this repository — audit it.

## What Seizr does and does not do

- **It never sees your password.** Sign-in uses Microsoft's **OAuth2 device-code
  flow**: you authenticate in your own browser on `microsoft.com`, and Microsoft
  hands Seizr a token. Your password never reaches this app. → see `seizr/auth.py`.
- **The Microsoft refresh token is stored encrypted.** After login, Seizr keeps
  your Microsoft *refresh token* so re-runs skip the browser step. Account
  passwords are hashed with **argon2**; Minecraft refresh tokens are **encrypted
  at rest** (Fernet/AES) with a key derived from `SEIZR_SECRET_KEY`. No plaintext
  passwords or tokens are stored.
  - Multi-user web server: per-user, in the `minecraft_accounts` table. → `seizr/db.py`, `seizr/crypto.py`
  - CLI / single-user: local `.auth_cache.json`, `chmod 600` (or `AUTH_CACHE` volume). → `seizr/auth.py`
- **It acts only on your own profile.** The only write Seizr makes is the name
  claim `PUT` on the account you signed into. → see `seizr/mojang.py`.
- **It talks to two hosts only:** `login.microsoftonline.com` (auth) and
  `api.minecraftservices.com` (Xbox/XSTS/Minecraft + the claim). No analytics, no
  third-party calls, no telemetry.

## Hosting safety

The web UI has **no built-in authentication** and operates your account. Do **not**
expose it to the public internet. Bind it to `127.0.0.1` and reach it over an SSH
tunnel, or put auth in front of it. See `DEPLOY.md`.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for an
unpatched vulnerability.

- Email: **TODO: your-security-contact@example.com**
- Or open a [GitHub Security Advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) (private).

Expect an acknowledgement within a few days. Please give a reasonable window to
fix before public disclosure.

## Scope

In scope: the auth chain, token storage, the web server (`seizr/web/`), and the
deployment guidance. Out of scope: Mojang/Microsoft's own APIs, and anything you
change in a fork.
