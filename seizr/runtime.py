"""Per-user web runtime: live event stream plus the snipe job runner.

Each logged-in user gets one UserRuntime (their Minecraft auth, MS-login
state, running snipe task, and a private event bus). The web routers create
and look these up via seizr.web.deps.get_runtime.
"""

import asyncio
import contextlib
from typing import Optional

import aiohttp

from seizr.auth import MinecraftAuth
from seizr.clock import DEFAULT_WINDOW_MINUTES, fmt_remaining, iso_utc, parse_drop_time, sync_clock
from seizr.db import Database
from seizr.mojang import NameSniper


class LogBus:
    """Fan-out of event dicts to every connected SSE client.

    Queues are bounded; a stalled client drops its oldest events instead of
    growing server memory without limit.
    """

    MAX_QUEUE = 500

    def __init__(self):
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self.subscribers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop oldest
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)


class UserRuntime:
    """Per-user runtime: their Minecraft auth, MS-login state, sniper job, and
    a private log/event stream. Keyed by user id so users never collide."""

    def __init__(self, auth: MinecraftAuth):
        self.auth = auth
        self.login = {"status": "idle"}  # idle|pending|done|error
        self.running = False
        self.task: Optional[asyncio.Task] = None  # the in-flight snipe, for cancel
        self.bus = LogBus()


async def run_snipe(session: aiohttp.ClientSession, db: Database,
                    rt: UserRuntime, user_id: int, params: dict) -> None:
    """Execute one snipe run end to end, streaming progress to rt.bus."""
    bus = rt.bus
    auth = rt.auth

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
        log(f"Drop window: {iso_utc(ws)} -> {iso_utc(we)}")

        # countdown to the window opening; keep the bearer fresh during the wait.
        # Hands off 15 s early so the sniper can measure its latency lead.
        while clock.now() < ws - 15:
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
                await db.save_mc_profile(user_id, name)
            bus.publish({"type": "done", "claimed": True, "name": name})
        else:
            bus.publish({"type": "done", "claimed": False})
    except asyncio.CancelledError:  # user hit Stop
        log("Stopped by user.")
        bus.publish({"type": "done", "claimed": False, "stopped": True})
        raise
    except Exception as exc:  # surface any failure to the UI
        bus.publish({"type": "done", "claimed": False, "error": str(exc)})
    finally:
        rt.running = False
        rt.task = None
