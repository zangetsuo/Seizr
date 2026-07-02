"""Minecraft (Microsoft) account link routes — device-code login per user."""

import asyncio

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from seizr.auth import AuthError
from seizr.runtime import UserRuntime
from seizr.web.deps import current_user, get_runtime, require_login

router = APIRouter()


async def _complete_login(app: FastAPI, rt: UserRuntime, user_id: int, dc: dict) -> None:
    try:
        oauth = await rt.auth.poll_device_token(dc)
        await rt.auth.finish_oauth(oauth)
        profile = await rt.auth.get_profile()
        name = profile["name"] if profile else None
        rt.login = {"status": "done", "profile": name}
        if name:
            await app.state.db.save_mc_profile(user_id, name)
    except AuthError as exc:
        rt.login = {"status": "error", "error": str(exc)}


@router.post("/api/login")
async def login(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    app = request.app
    rt = await get_runtime(request, user["id"])

    if rt.login.get("status") == "done":
        return {"status": "done", "profile": rt.login.get("profile")}

    # try the stored (per-user) refresh token silently first
    if rt.auth.refresh_token:
        try:
            await rt.auth.refresh_login()
            profile = await rt.auth.get_profile()
            name = profile["name"] if profile else None
            rt.login = {"status": "done", "profile": name}
            if name:
                await app.state.db.save_mc_profile(user["id"], name)
            return {"status": "done", "profile": name}
        except AuthError:
            pass  # fall through to device code

    dc = await rt.auth.begin_device_code()
    rt.login = {
        "status": "pending",
        "user_code": dc["user_code"],
        "verification_uri": dc["verification_uri"],
    }
    asyncio.create_task(_complete_login(app, rt, user["id"], dc))
    return {
        "status": "pending",
        "user_code": dc["user_code"],
        "verification_uri": dc["verification_uri"],
    }


@router.get("/api/status")
async def status(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    rt = await get_runtime(request, user["id"])
    return {"login": rt.login, "running": rt.running}


@router.post("/api/mc-logout")
async def mc_logout(request: Request):
    """Disconnect the Minecraft/Microsoft account so a different one can sign in.

    Wipes the stored refresh token and resets login state; the app account
    (this Seizr profile) stays signed in.
    """
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    rt = await get_runtime(request, user["id"])
    if rt.running:
        return JSONResponse({"error": "Stop the running snipe first."}, status_code=409)
    await request.app.state.db.clear_mc(user["id"])
    rt.auth.forget()
    rt.login = {"status": "idle"}
    return {"ok": True}
