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
        concurrent_requests: int = 20,
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

    async def is_available(self) -> Optional[bool]:
        """True/False, or None on transient error (so the caller keeps polling)."""
        url = AVAILABLE_URL.format(name=self.target)
        try:
            async with self.session.get(url, headers=await self._headers()) as resp:
                if resp.status == 429:
                    self._log("availability: 429 rate limited — backing off")
                    return None
                if resp.status != 200:
                    self._log(f"availability: HTTP {resp.status}")
                    return None
                data = await resp.json()
        except aiohttp.ClientError as exc:
            self._log(f"availability: network error {exc}")
            return None
        return data.get("status") == "AVAILABLE"

    async def poll_until_available(self, interval_ms: int, deadline: float) -> None:
        """Poll availability until AVAILABLE or until deadline (epoch seconds)."""
        interval = interval_ms / 1000
        while self.now() < deadline:
            status = await self.is_available()
            if status is True:
                self._log(f"'{self.target}' is AVAILABLE")
                return
            await asyncio.sleep(interval)
        self._log("reached drop time — proceeding to burst")

    # ----- claim burst -----------------------------------------------------

    async def _single_claim(self, n: int) -> bool:
        """One PUT. Returns True on a 200 (claimed)."""
        url = CLAIM_URL.format(name=self.target)
        try:
            async with self.session.put(url, headers=await self._headers()) as resp:
                code = resp.status
                if code == 200:
                    self._log(f"req#{n}: HTTP 200 — CLAIMED")
                    return True
                if code == 429:
                    self._log(f"req#{n}: HTTP 429 rate limited")
                elif code == 403:
                    self._log(f"req#{n}: HTTP 403 (not available / cooldown)")
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

    async def claim_burst(self, give_up_at: float) -> bool:
        """Repeat bursts from now until a 200 or until give_up_at."""
        self._log(f"BURST start — {self.concurrent} concurrent PUTs per round")
        while not self._stop.is_set() and self.now() < give_up_at:
            if await self._burst():
                self.claimed = True
                return True
        if not self.claimed:
            self._log("gave up — name not claimed in window")
        return self.claimed
