"""Availability polling and the name-claim burst.

All network calls share the aiohttp session from sniper.py and the bearer token
from auth.py. Timestamps in logs come from the NTP-corrected clock so they line
up with the real drop time, not the (possibly skewed) system clock.
"""

import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional

LogFn = Callable[[str], None]

import aiohttp

from auth import MinecraftAuth

AVAILABLE_URL = "https://api.minecraftservices.com/minecraft/profile/name/{name}/available"
CLAIM_URL = "https://api.minecraftservices.com/minecraft/profile/name/{name}"
NAMECHANGE_URL = "https://api.minecraftservices.com/minecraft/profile/namechange"


class NameSniper:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: MinecraftAuth,
        target_name: str,
        now_fn: Callable[[], float],
        concurrent_requests: int = 5,
        log_fn: Optional[LogFn] = None,
    ):
        self.session = session
        self.auth = auth
        self.target = target_name
        self.now = now_fn  # NTP-corrected epoch seconds
        self.concurrent = concurrent_requests
        self.log_fn = log_fn  # optional extra sink (e.g. web UI stream)
        self._stop = asyncio.Event()
        self.claimed = False

    def _ts(self) -> str:
        return datetime.fromtimestamp(self.now(), tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]

    def _log(self, msg: str) -> None:
        line = f"[{self._ts()}] {msg}"
        print(line)
        if self.log_fn:
            self.log_fn(line)

    async def _headers(self) -> dict:
        bearer = await self.auth.get_bearer()
        return {"Authorization": f"Bearer {bearer}"}

    async def _claim_headers(self) -> dict:
        # The name-change PUT rejects requests with no Content-Type (HTTP 415),
        # so send application/json even though the body is empty.
        headers = await self._headers()
        headers["Content-Type"] = "application/json"
        return headers

    # ----- cooldown guard --------------------------------------------------

    async def check_no_cooldown(self) -> bool:
        """True if the account may change its name right now."""
        async with self.session.get(NAMECHANGE_URL, headers=await self._headers()) as resp:
            if resp.status != 200:
                self._log(f"namechange check failed: HTTP {resp.status}")
                return False
            data = await resp.json()
        allowed = data.get("nameChangeAllowed", False)
        if allowed:
            self._log("Name change allowed — no active cooldown.")
        else:
            self._log(f"COOLDOWN active. changedAt={data.get('changedAt')}. Cannot snipe yet.")
        return allowed

    # ----- availability ----------------------------------------------------

    async def is_available(self) -> str:
        """Return one of: AVAILABLE, TAKEN, RATELIMIT, ERROR.

        RATELIMIT is distinct so the poll loop can back off instead of hammering.
        """
        url = AVAILABLE_URL.format(name=self.target)
        try:
            async with self.session.get(url, headers=await self._headers()) as resp:
                if resp.status == 429:
                    return "RATELIMIT"
                if resp.status != 200:
                    self._log(f"availability: HTTP {resp.status}")
                    return "ERROR"
                data = await resp.json()
        except aiohttp.ClientError as exc:
            self._log(f"availability: network error {exc}")
            return "ERROR"
        return "AVAILABLE" if data.get("status") == "AVAILABLE" else "TAKEN"

    # ----- claim burst -----------------------------------------------------

    async def _single_claim(self, n: int) -> bool:
        """One PUT. Returns True on a 200 (claimed)."""
        url = CLAIM_URL.format(name=self.target)
        try:
            async with self.session.put(url, headers=await self._claim_headers()) as resp:
                code = resp.status
                if code == 200:
                    self._log(f"req#{n}: HTTP 200 — CLAIMED")
                    return True
                if code == 429:
                    self._log(f"req#{n}: HTTP 429 rate limited")
                elif code == 403:
                    self._log(f"req#{n}: HTTP 403 (not available / cooldown)")
                elif code == 415:
                    self._log(f"req#{n}: HTTP 415 (bad content-type)")
                else:
                    self._log(f"req#{n}: HTTP {code}")
        except aiohttp.ClientError as exc:
            self._log(f"req#{n}: network error {exc}")
        return False

    async def _burst(self) -> bool:
        """Fire `concurrent` PUTs at once. Stop on first 200."""
        tasks = [asyncio.create_task(self._single_claim(i + 1)) for i in range(self.concurrent)]
        won = False
        for fut in asyncio.as_completed(tasks):
            if await fut:
                won = True
                self._stop.set()
                break
        # cancel any still-in-flight requests once we have a winner
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return won

    # ----- window snipe ----------------------------------------------------

    # On an AVAILABLE flip, fire this many back-to-back burst rounds before
    # falling back to polling (covers losing the first race / brief 429s).
    AGGRESSIVE_ROUNDS = 6
    # Adaptive poll backoff: double the interval on each 429, cap here, reset on
    # any clean response. Keeps us under the limit so we stay responsive — and
    # avoids the 429 flood that can temp-suspend the account.
    MAX_BACKOFF_S = 10.0

    async def snipe_window(self, window_start: float, window_end: float, poll_interval_ms: int) -> bool:
        """Poll the whole drop window; burst-claim on the first AVAILABLE flip.

        Names no longer drop at an exact instant — they free up somewhere inside
        a window. So we poll continuously from window_start to window_end and, the
        moment availability flips, hammer claims until we win or the name is taken.
        Polling backs off on HTTP 429 and recovers once the limit clears.
        """
        base = poll_interval_ms / 1000
        interval = base

        while self.now() < window_start:
            await asyncio.sleep(min(base, max(0.0, window_start - self.now())))

        self._log(f"window open — polling '{self.target}' every {poll_interval_ms}ms "
                  f"until window close")

        checks = 0
        while not self._stop.is_set() and self.now() < window_end:
            status = await self.is_available()
            checks += 1

            if status == "AVAILABLE":
                self._log(f"check #{checks}: '{self.target}' AVAILABLE — firing bursts")
                for _ in range(self.AGGRESSIVE_ROUNDS):
                    if await self._burst():
                        self.claimed = True
                        return True
                    if self.now() >= window_end:
                        break
                # not won this flip (someone may hold it briefly) — resume polling
                interval = base
            elif status == "RATELIMIT":
                interval = min(interval * 2, self.MAX_BACKOFF_S)
                self._log(f"check #{checks}: 429 rate limited — backing off to {interval:.1f}s")
            elif status == "TAKEN":
                self._log(f"check #{checks}: '{self.target}' still taken")
                interval = base
            else:  # ERROR
                self._log(f"check #{checks}: availability check error")
                interval = base

            await asyncio.sleep(interval)

        if not self.claimed:
            self._log("window closed — name not claimed")
        return self.claimed
