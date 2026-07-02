"""Seizr — seize the moment a Minecraft name drops.

Package layout:
    clock.py    NTP-corrected clock + drop-window time parsing
    auth.py     Microsoft -> Xbox -> XSTS -> Minecraft auth chain
    mojang.py   Availability polling and the claim burst (the hot path)
    db.py       SQLite data layer (users, sessions, tokens, MC accounts)
    accounts.py Register/login/session logic + Google OAuth
    crypto.py   At-rest encryption for stored refresh tokens
    mailer.py   Verification email (SMTP or dev console fallback)
    namemc.py   Best-effort NameMC drop-window scrape (optional Playwright)
    runtime.py  Per-user web runtime: event bus + snipe job runner
    cli.py      Terminal entry point (config.json driven)
    web/        FastAPI app, split into routers
"""

from pathlib import Path

# Repo root (parent of this package). Default location for seizr.db,
# .auth_cache.json, config.json and the static/ dir; env vars override.
ROOT_DIR = Path(__file__).resolve().parent.parent
