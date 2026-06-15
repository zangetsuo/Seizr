"""Account logic: password hashing, register/login, session + email tokens,
and Google OAuth. Talks to the DB layer in db.py; holds no SQL itself.
"""

import os
import re
import secrets
from typing import Optional

import aiohttp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from db import Database

_ph = PasswordHasher()

SESSION_TTL = 60 * 60 * 24 * 30      # 30 days
EMAIL_TOKEN_TTL = 60 * 60 * 24       # 24 hours
PASSWORD_MIN = 8

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Google OAuth (set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET to enable).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class AccountError(Exception):
    """Raised on a user-facing account problem (message is safe to show)."""


# ----- validation ----------------------------------------------------------

def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email or ""))


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ----- email + password -----------------------------------------------------

async def register_local(db: Database, email: str, password: str) -> tuple[int, str]:
    """Create a local account. Returns (user_id, verification_token)."""
    email = (email or "").strip().lower()
    if not valid_email(email):
        raise AccountError("Enter a valid email address.")
    if len(password or "") < PASSWORD_MIN:
        raise AccountError(f"Password must be at least {PASSWORD_MIN} characters.")
    if await db.get_user_by_email(email):
        raise AccountError("An account with that email already exists.")

    user_id = await db.create_user(
        email=email,
        password_hash=_ph.hash(password),
        display_name=email.split("@")[0],
        email_verified=False,
    )
    token = new_token()
    await db.create_email_token(token, user_id, "verify", EMAIL_TOKEN_TTL)
    return user_id, token


async def login_local(db: Database, email: str, password: str):
    """Return the user row on valid credentials, else None."""
    user = await db.get_user_by_email((email or "").strip().lower())
    if not user or not user["password_hash"]:
        return None
    try:
        _ph.verify(user["password_hash"], password or "")
    except VerifyMismatchError:
        return None
    return user


# ----- sessions -------------------------------------------------------------

async def start_session(db: Database, user_id: int) -> str:
    token = new_token()
    await db.create_session(token, user_id, SESSION_TTL)
    return token


# ----- Google OAuth ---------------------------------------------------------

def google_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def google_auth_url(redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def google_exchange(session: aiohttp.ClientSession, code: str, redirect_uri: str) -> dict:
    """Exchange an auth code for tokens, then fetch the user's profile."""
    async with session.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }) as resp:
        if resp.status != 200:
            raise AccountError("Google sign-in failed.")
        tokens = await resp.json()

    async with session.get(GOOGLE_USERINFO_URL, headers={
        "Authorization": f"Bearer {tokens['access_token']}"
    }) as resp:
        if resp.status != 200:
            raise AccountError("Could not read your Google profile.")
        return await resp.json()  # {sub, email, email_verified, name, ...}


async def google_upsert_user(db: Database, info: dict):
    """Find or create a user from a Google profile. Returns the user row."""
    sub = info.get("sub")
    email = (info.get("email") or "").lower()
    user = await db.get_user_by_google_sub(sub)
    if user:
        return user
    # link to an existing local account with the same email, else create one
    existing = await db.get_user_by_email(email) if email else None
    if existing:
        await db.link_google_sub(existing["id"], sub)
        return await db.get_user(existing["id"])
    user_id = await db.create_user(
        email=email,
        google_sub=sub,
        display_name=info.get("name") or (email.split("@")[0] if email else "user"),
        email_verified=bool(info.get("email_verified")),  # Google emails are pre-verified
    )
    return await db.get_user(user_id)
