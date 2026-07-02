"""Snipe control routes: start/stop a run, drop-window lookup, live SSE feed."""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from seizr.namemc import DropLookupError, fetch_drop_info
from seizr.runtime import run_snipe
from seizr.web.deps import current_user, get_runtime, require_login

router = APIRouter()

# Keep proxies (Caddy/nginx) from dropping an idle SSE stream: send a comment
# ping whenever no real event arrives within this many seconds.
SSE_PING_S = 15


@router.post("/api/start")
async def start(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    app = request.app
    rt = await get_runtime(request, user["id"])
    if rt.login.get("status") != "done":
        return JSONResponse({"error": "Connect your Minecraft account first."}, status_code=400)
    if rt.running:
        return JSONResponse({"error": "A run is already in progress."}, status_code=409)

    params = await request.json()
    if not params.get("target") or not params.get("window_start"):
        return JSONResponse({"error": "target and window_start required"}, status_code=400)

    rt.task = asyncio.create_task(
        run_snipe(app.state.session, app.state.db, rt, user["id"], params)
    )
    return {"ok": True}


@router.post("/api/stop")
async def stop(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    rt = await get_runtime(request, user["id"])
    if not rt.running or not rt.task:
        return JSONResponse({"error": "Nothing is running."}, status_code=409)
    rt.task.cancel()
    return {"ok": True}


@router.get("/api/droptime")
async def droptime(request: Request, name: str = ""):
    """Best-effort NameMC drop-window lookup. Returns ISO start/end (UTC)."""
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    try:
        return await fetch_drop_info(name.strip())
    except DropLookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/api/events")
async def events(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    rt = await get_runtime(request, user["id"])
    q = rt.bus.subscribe()

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=SSE_PING_S)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            rt.bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")
