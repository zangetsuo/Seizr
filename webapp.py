"""Web UI for the Seizr name sniper.

Run locally:
    .venv/bin/python -m uvicorn webapp:app --port 8000
    # then open http://localhost:8000

In a container / cloud VM:
    uvicorn webapp:app --host 0.0.0.0 --port 8000

Wraps auth.py + api.py + sniper.py. The frontend lives in static/ (landing.html
+ app.html) and talks to this server: log in (Microsoft device code), enter the
target + drop window, then run with a live log + countdown streamed (SSE).
"""

import asyncio
import contextlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

import accounts
from accounts import AccountError
from api import NameSniper
from auth import AuthError, MinecraftAuth
from db import Database
from mailer import send_verification
from sniper import (
    DEFAULT_WINDOW_MINUTES,
    fmt_remaining,
    parse_drop_time,
    sync_clock,
)

STATIC_DIR = Path(__file__).with_name("static")
SESSION_COOKIE = "seizr_session"
GSTATE_COOKIE = "seizr_gstate"
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "0") == "1"  # set 1 behind HTTPS


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


class UserRuntime:
    """Per-user runtime: their Minecraft auth, MS-login state, sniper job, and
    a private log/event stream. Keyed by user id so users never collide."""

    def __init__(self, auth: MinecraftAuth):
        self.auth = auth
        self.login = {"status": "idle"}  # idle|pending|done|error
        self.running = False
        self.bus = LogBus()


async def get_runtime(app: FastAPI, user_id: int) -> UserRuntime:
    rt = app.state.runtimes.get(user_id)
    if rt:
        return rt
    db: Database = app.state.db
    token = await db.get_mc_refresh(user_id)

    async def on_refresh(new_token: str) -> None:
        await db.save_mc_refresh(user_id, new_token)

    auth = MinecraftAuth(app.state.session, refresh_token=token, on_refresh=on_refresh)
    rt = UserRuntime(auth)
    app.state.runtimes[user_id] = rt
    return rt


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.session = aiohttp.ClientSession()
    app.state.db = await Database.connect()
    app.state.runtimes = {}  # user_id -> UserRuntime
    yield
    await app.state.db.close()
    await app.state.session.close()


app = FastAPI(lifespan=lifespan)


# ----- session helpers -----------------------------------------------------

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


def require_login(user) -> JSONResponse | None:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return None


# ----- account auth (Seizr login/register) ---------------------------------

@app.get("/api/me")
async def me(request: Request):
    user = await current_user(request)
    if not user:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "email": user["email"],
        "display_name": user["display_name"],
        "email_verified": bool(user["email_verified"]),
    }


@app.post("/auth/register")
async def register(request: Request):
    db: Database = request.app.state.db
    data = await request.json()
    try:
        user_id, token = await accounts.register_local(
            db, data.get("email", ""), data.get("password", "")
        )
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    link = f"{str(request.base_url).rstrip('/')}/auth/verify?token={token}"
    try:
        await send_verification(data["email"].strip().lower(), link)
    except Exception as exc:  # account exists; don't fail signup on a mail hiccup
        print(f"WARNING: verification email failed to send: {exc}")
    return {"ok": True, "message": "Check your email to verify your account."}


@app.get("/auth/verify")
async def verify(request: Request, token: str = ""):
    db: Database = request.app.state.db
    user_id = await db.consume_email_token(token, "verify")
    if not user_id:
        return RedirectResponse("/login?error=verify", status_code=303)
    await db.set_email_verified(user_id)
    sess = await accounts.start_session(db, user_id)
    resp = RedirectResponse("/app", status_code=303)
    set_session_cookie(resp, sess)
    return resp


@app.post("/auth/login")
async def auth_login(request: Request):
    db: Database = request.app.state.db
    data = await request.json()
    user = await accounts.login_local(db, data.get("email", ""), data.get("password", ""))
    if not user:
        return JSONResponse({"error": "Invalid email or password."}, status_code=401)
    if not user["email_verified"]:
        return JSONResponse({"error": "Verify your email first.", "code": "unverified"},
                            status_code=403)
    sess = await accounts.start_session(db, user["id"])
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, sess)
    return resp


@app.post("/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await request.app.state.db.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/auth/google")
async def google_start(request: Request):
    if not accounts.google_enabled():
        return RedirectResponse("/login?error=google_off", status_code=303)
    redirect_uri = f"{str(request.base_url).rstrip('/')}/auth/google/callback"
    state = accounts.new_token()
    resp = RedirectResponse(accounts.google_auth_url(redirect_uri, state), status_code=303)
    resp.set_cookie(GSTATE_COOKIE, state, max_age=600, httponly=True,
                    samesite="lax", secure=SECURE_COOKIES, path="/")
    return resp


@app.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    db: Database = request.app.state.db
    if not code or not state or state != request.cookies.get(GSTATE_COOKIE):
        return RedirectResponse("/login?error=google", status_code=303)
    redirect_uri = f"{str(request.base_url).rstrip('/')}/auth/google/callback"
    try:
        info = await accounts.google_exchange(request.app.state.session, code, redirect_uri)
        user = await accounts.google_upsert_user(db, info)
    except AccountError:
        return RedirectResponse("/login?error=google", status_code=303)
    sess = await accounts.start_session(db, user["id"])
    resp = RedirectResponse("/app", status_code=303)
    set_session_cookie(resp, sess)
    resp.delete_cookie(GSTATE_COOKIE, path="/")
    return resp


# ----- Minecraft (Microsoft) link, per user --------------------------------

async def _complete_login(app: FastAPI, rt: "UserRuntime", user_id: int, dc: dict) -> None:
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


@app.post("/api/login")
async def login(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    app = request.app
    rt = await get_runtime(app, user["id"])

    if rt.login.get("status") == "done":
        return {"status": "done", "profile": rt.login.get("profile")}

    # try the stored (per-user) refresh token silently first
    if rt.auth.refresh_token:
        try:
            await rt.auth._run_chain(use_refresh=True)
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


@app.get("/api/status")
async def status(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    rt = await get_runtime(request.app, user["id"])
    return {"login": rt.login, "running": rt.running}


# ----- sniper run ---------------------------------------------------------

async def _run_snipe(app: FastAPI, rt: "UserRuntime", user_id: int, params: dict) -> None:
    bus: LogBus = rt.bus
    auth: MinecraftAuth = rt.auth
    session: aiohttp.ClientSession = app.state.session

    def log(msg: str) -> None:
        bus.publish({"type": "log", "msg": msg})

    rt.running = True
    try:
        clock = await sync_clock(params.get("ntp_server", "pool.ntp.org"))
        log(f"NTP offset {clock.offset * 1000:.0f} ms")

        target = params["target"]
        sniper = NameSniper(
            session, auth, target, clock.now,
            concurrent_requests=int(params.get("concurrent", 5)),
            log_fn=log,
        )

        if not await sniper.check_no_cooldown():
            bus.publish({"type": "done", "claimed": False, "error": "active name-change cooldown"})
            return

        ws = parse_drop_time(params["window_start"])
        we = (parse_drop_time(params["window_end"]) if params.get("window_end")
              else ws + DEFAULT_WINDOW_MINUTES * 60)
        poll_ms = int(params.get("poll_ms", 1000))
        iso = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
        log(f"Drop window: {iso(ws)} -> {iso(we)}")

        # countdown to the window opening; keep the bearer fresh during the wait
        while clock.now() < ws:
            rem = ws - clock.now()
            bus.publish({"type": "countdown", "remaining": rem, "label": fmt_remaining(rem),
                         "phase": "opens"})
            await auth.get_bearer()
            await asyncio.sleep(1)

        # window open: poll + burst while a ticker streams time left in the window
        async def ticker():
            while True:
                rem = we - clock.now()
                bus.publish({"type": "countdown", "remaining": rem,
                             "label": fmt_remaining(rem), "phase": "closes"})
                await asyncio.sleep(1)

        tick = asyncio.create_task(ticker())
        try:
            claimed = await sniper.snipe_window(ws, we, poll_ms)
        finally:
            tick.cancel()

        if claimed:
            profile = await auth.get_profile()
            name = profile["name"] if profile else target
            if profile:
                await app.state.db.save_mc_profile(user_id, name)
            bus.publish({"type": "done", "claimed": True, "name": name})
        else:
            bus.publish({"type": "done", "claimed": False})
    except Exception as exc:  # surface any failure to the UI
        bus.publish({"type": "done", "claimed": False, "error": str(exc)})
    finally:
        rt.running = False


@app.post("/api/start")
async def start(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    app = request.app
    rt = await get_runtime(app, user["id"])
    if rt.login.get("status") != "done":
        return JSONResponse({"error": "Connect your Minecraft account first."}, status_code=400)
    if rt.running:
        return JSONResponse({"error": "A run is already in progress."}, status_code=409)

    params = await request.json()
    if not params.get("target") or not params.get("window_start"):
        return JSONResponse({"error": "target and window_start required"}, status_code=400)

    asyncio.create_task(_run_snipe(app, rt, user["id"], params))
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request):
    user = await current_user(request)
    if guard := require_login(user):
        return guard
    rt = await get_runtime(request.app, user["id"])
    q = rt.bus.subscribe()

    async def stream():
        try:
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            rt.bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
async def landing():
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/register")
async def register_page():
    return FileResponse(STATIC_DIR / "register.html")


@app.get("/app")
async def webapp_page(request: Request):
    if not await current_user(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "app.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
