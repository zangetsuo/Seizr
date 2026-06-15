"""Web UI for the Kairos name sniper.

Run locally:
    .venv/bin/python -m uvicorn webapp:app --port 8000
    # then open http://localhost:8000

In a container / cloud VM:
    uvicorn webapp:app --host 0.0.0.0 --port 8000

Wraps auth.py + api.py + sniper.py. The frontend lives in static/index.html and
talks to this server: log in (Microsoft device code), enter target + drop time,
jump to NameMC, then run the sniper with a live log + countdown streamed (SSE).
"""

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from api import NameSniper
from auth import AuthError, MinecraftAuth
from sniper import (
    GIVE_UP_AFTER_SECONDS,
    POLL_LEAD_SECONDS,
    fmt_remaining,
    parse_drop_time,
    sync_clock,
)

STATIC_DIR = Path(__file__).with_name("static")


class LogBus:
    """Fan-out of event dicts to every connected SSE client."""

    def __init__(self):
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait(event)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.session = aiohttp.ClientSession()
    app.state.auth = MinecraftAuth(app.state.session)
    app.state.bus = LogBus()
    app.state.login = {"status": "idle"}  # idle|pending|done|error
    app.state.running = False
    yield
    await app.state.session.close()


app = FastAPI(lifespan=lifespan)


# ----- login -------------------------------------------------------------

async def _complete_login(app: FastAPI, dc: dict) -> None:
    auth: MinecraftAuth = app.state.auth
    try:
        oauth = await auth.poll_device_token(dc)
        await auth.finish_oauth(oauth)
        profile = await auth.get_profile()
        app.state.login = {
            "status": "done",
            "profile": profile["name"] if profile else None,
        }
    except AuthError as exc:
        app.state.login = {"status": "error", "error": str(exc)}


@app.post("/api/login")
async def login(request: Request):
    app = request.app
    auth: MinecraftAuth = app.state.auth

    if app.state.login.get("status") == "done":
        return {"status": "done", "profile": app.state.login.get("profile")}

    # try cached refresh token silently first
    auth._load_cache()
    if auth.refresh_token:
        try:
            await auth._run_chain(use_refresh=True)
            profile = await auth.get_profile()
            app.state.login = {"status": "done", "profile": profile["name"] if profile else None}
            return {"status": "done", "profile": app.state.login["profile"]}
        except AuthError:
            pass  # fall through to device code

    dc = await auth.begin_device_code()
    app.state.login = {
        "status": "pending",
        "user_code": dc["user_code"],
        "verification_uri": dc["verification_uri"],
    }
    asyncio.create_task(_complete_login(app, dc))
    return {
        "status": "pending",
        "user_code": dc["user_code"],
        "verification_uri": dc["verification_uri"],
    }


@app.get("/api/status")
async def status(request: Request):
    app = request.app
    return {"login": app.state.login, "running": app.state.running}


# ----- sniper run ---------------------------------------------------------

async def _run_snipe(app: FastAPI, params: dict) -> None:
    bus: LogBus = app.state.bus
    auth: MinecraftAuth = app.state.auth
    session: aiohttp.ClientSession = app.state.session

    def log(msg: str) -> None:
        bus.publish({"type": "log", "msg": msg})

    app.state.running = True
    try:
        clock = await sync_clock(params.get("ntp_server", "pool.ntp.org"))
        log(f"NTP offset {clock.offset * 1000:.0f} ms")

        target = params["target"]
        sniper = NameSniper(
            session, auth, target, clock.now,
            concurrent_requests=int(params.get("concurrent", 20)),
            log_fn=log,
        )

        if not await sniper.check_no_cooldown():
            bus.publish({"type": "done", "claimed": False, "error": "active name-change cooldown"})
            return

        drop = parse_drop_time(params["drop_time"])
        log(f"Drop time: {datetime.fromtimestamp(drop, tz=timezone.utc).isoformat()}")

        burst_s = int(params.get("burst_s", 2))
        poll_ms = int(params.get("poll_ms", 500))
        burst_at = drop - burst_s

        # live countdown until polling window (T-3min)
        poll_start = drop - POLL_LEAD_SECONDS
        while clock.now() < poll_start:
            remaining = drop - clock.now()
            bus.publish({"type": "countdown", "remaining": remaining,
                         "label": fmt_remaining(remaining)})
            await auth.get_bearer()  # keep bearer fresh during long waits
            await asyncio.sleep(1)

        log(f"Polling '{target}' every {poll_ms}ms until T-{burst_s}s")

        async def poll_loop():
            while clock.now() < burst_at:
                bus.publish({"type": "countdown", "remaining": drop - clock.now(),
                             "label": fmt_remaining(drop - clock.now())})
                if await sniper.is_available() is True:
                    log(f"'{target}' is AVAILABLE")
                    return
                await asyncio.sleep(poll_ms / 1000)

        await poll_loop()
        while clock.now() < burst_at:
            await asyncio.sleep(0.05)

        claimed = await sniper.claim_burst(give_up_at=drop + GIVE_UP_AFTER_SECONDS)
        if claimed:
            profile = await auth.get_profile()
            name = profile["name"] if profile else target
            bus.publish({"type": "done", "claimed": True, "name": name})
        else:
            bus.publish({"type": "done", "claimed": False})
    except Exception as exc:  # surface any failure to the UI
        bus.publish({"type": "done", "claimed": False, "error": str(exc)})
    finally:
        app.state.running = False


@app.post("/api/start")
async def start(request: Request):
    app = request.app
    if app.state.login.get("status") != "done":
        return JSONResponse({"error": "not logged in"}, status_code=400)
    if app.state.running:
        return JSONResponse({"error": "already running"}, status_code=409)

    params = await request.json()
    if not params.get("target") or not params.get("drop_time"):
        return JSONResponse({"error": "target and drop_time required"}, status_code=400)

    asyncio.create_task(_run_snipe(app, params))
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request):
    bus: LogBus = request.app.state.bus
    q = bus.subscribe()

    async def stream():
        try:
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
