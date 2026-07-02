"""Shared request helpers for the web routers: session cookie handling,
login guards, and per-user runtime lookup."""

import os

from fastapi import Request
from fastapi.responses import JSONResponse

from seizr import accounts
from seizr.auth import MinecraftAuth
from seizr.db import Database
from seizr.runtime import UserRuntime

SESSION_COOKIE = "seizr_session"
GSTATE_COOKIE = "seizr_gstate"
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "0") == "1"  # set 1 behind HTTPS


async def current_user(request: Request):
    """Return the logged-in user row, or None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return await request.app.state.db.get_session_user(token)


def set_session_cookie(resp, token: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=accounts.SESSION_TTL, httponly=True,
        samesite="lax", secure=SECURE_COOKIES, path="/",
    )


def require_login(user):
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return None


async def get_runtime(request: Request, user_id: int) -> UserRuntime:
    """Fetch (or lazily build) the per-user runtime."""
    runtimes: dict = request.app.state.runtimes
    rt = runtimes.get(user_id)
    if rt:
        return rt
    db: Database = request.app.state.db
    token = await db.get_mc_refresh(user_id)

    async def on_refresh(new_token: str) -> None:
        await db.save_mc_refresh(user_id, new_token)

    auth = MinecraftAuth(request.app.state.session, refresh_token=token, on_refresh=on_refresh)
    # another request may have built the runtime while we awaited the DB;
    # setdefault keeps exactly one per user
    return runtimes.setdefault(user_id, UserRuntime(auth))
