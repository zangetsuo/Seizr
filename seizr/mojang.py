"""Availability polling and the name-claim burst — the timing-critical path.

All network calls share one aiohttp session (warm TLS connections) and the
bearer token from auth.py. Timestamps in logs come from the NTP-corrected
clock so they line up with the real drop time, not the system clock.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import aiohttp

from seizr.auth import MinecraftAuth

LogFn = Callable[[str], None]

AVAILABLE_URL = "https://api.minecraftservices.com/minecraft/profile/name/{name}/available"
CLAIM_URL = "https://api.minecraftservices.com/minecraft/profile/name/{name}"
NAMECHANGE_URL = "https://api.minecraftservices.com/minecraft/profile/namechange"


class TokenBucket:
    """Global request-rate governor shared by availability GETs and claim PUTs.

    Mojang meters both against the account, so they must draw from one budget.
    `capacity` tokens are available instantly for the burst at the drop; after
    that, requests are paced at `rate` per second. This bounds worst-case
    request volume no matter how long the window is or how often availability
    flips — the flood that risks a temp claim-lock can't happen.
    """

    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.monotonic()
        self._lock = asyncio.Lock()  # FIFO, so waiters are served in order

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.rate)


class NameSniper:
    # On an AVAILABLE flip, fire this many burst rounds before falling back to
    # polling (covers losing the first race / brief 429s).
    AGGRESSIVE_ROUNDS = 6
    # Adaptive poll backoff (AIMD): double the interval on each 429, halve it
    # per clean response. Snapping straight back to base after a 429 episode
    # re-triggers the limit immediately — Mojang's quota refills slowly and
    # repeat 429s extend the penalty — so recovery must be gradual. The cap
    # must sit above the penalty window or the loop never escapes it.
    MAX_BACKOFF_S = 60.0
    # Floor for the post-429 interval when the server sent no Retry-After.
    # Mojang's penalty window outlasts sub-second retries — doubling up from a
    # 300ms base burns 4+ requests in the first seconds, each extending the
    # penalty. Jump straight to a wait long enough to let quota refill.
    BACKOFF_FLOOR_S = 5.0
    # Shared budget: burst `capacity` requests at the drop, then sustain `rate`/s.
    BUCKET_RATE = 3.0
    BUCKET_CAPACITY = 8.0
    # Spacing between PUTs inside one burst. The rate limiter refills
    # continuously, so requests spread across the refill land more 200-eligible
    # slots than a simultaneous dump that all hits the same refused instant.
    STAGGER_S = 0.015
    # Pause between aggressive rounds, so round 2+ isn't wasted on the 429 wall
    # raised by round 1.
    ROUND_DELAY_S = 0.25
    # Windows at most this long are sniped PUT-first: every probe IS a claim
    # (200 = won, 403 = not yet), eliminating the GET round-trip between
    # detecting the flip and the first claim. Longer windows would accumulate
    # too many claim PUTs, so they keep the cheaper GET poll + burst-on-flip.
    PUT_PROBE_MAX_WINDOW_S = 120.0
    # Latency-lead: the first probe is sent this early (measured one-way trip)
    # so it ARRIVES at window_start instead of departing then. Early probes are
    # harmless (403/TAKEN). Capped so a bad measurement can't fire way early.
    LATENCY_SAMPLES = 3
    MAX_LEAD_S = 0.5

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
        self.bucket = TokenBucket(self.BUCKET_RATE, self.BUCKET_CAPACITY)
        self._stop = asyncio.Event()
        self.claimed = False
        # Server-mandated wait from the last 429's Retry-After header (seconds),
        # 0.0 when the header was absent. Read by the poll loops' backoff.
        self.retry_after = 0.0
        self.available_url = AVAILABLE_URL.format(name=target_name)
        self.claim_url = CLAIM_URL.format(name=target_name)

    def _ts(self) -> str:
        return datetime.fromtimestamp(self.now(), tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]

    def _log(self, msg: str) -> None:
        line = f"[{self._ts()}] {msg}"
        print(line)
        if self.log_fn:
            self.log_fn(line)

    def _note_retry_after(self, resp: aiohttp.ClientResponse) -> None:
        """Record the Retry-After header (seconds) from a 429 response."""
        try:
            self.retry_after = float(resp.headers.get("Retry-After", 0))
        except ValueError:  # HTTP-date form or garbage — ignore
            self.retry_after = 0.0
        if self.retry_after:
            self._log(f"server asked to retry after {self.retry_after:.0f}s")

    def _backoff(self, interval: float) -> float:
        """Next poll interval after a 429: honor Retry-After when the server
        sent one, otherwise double; capped either way."""
        wait = max(interval * 2, self.retry_after, self.BACKOFF_FLOOR_S)
        self.retry_after = 0.0
        return min(wait, self.MAX_BACKOFF_S)

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
        await self.bucket.acquire()
        try:
            async with self.session.get(self.available_url, headers=await self._headers()) as resp:
                if resp.status == 429:
                    self._note_retry_after(resp)
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

    async def _single_claim(self, n: int, headers: dict, delay: float = 0.0) -> int:
        """One PUT, gated by the shared rate budget. Returns the HTTP status
        (0 on network error)."""
        if delay:
            await asyncio.sleep(delay)
        await self.bucket.acquire()
        try:
            async with self.session.put(self.claim_url, headers=headers) as resp:
                code = resp.status
                if code == 200:
                    self._log(f"req#{n}: HTTP 200 — CLAIMED")
                elif code == 429:
                    self._log(f"req#{n}: HTTP 429 rate limited")
                    self._note_retry_after(resp)
                elif code == 403:
                    self._log(f"req#{n}: HTTP 403 (not available / cooldown)")
                elif code == 415:
                    self._log(f"req#{n}: HTTP 415 (bad content-type)")
                else:
                    self._log(f"req#{n}: HTTP {code}")
                return code
        except aiohttp.ClientError as exc:
            self._log(f"req#{n}: network error {exc}")
            return 0

    async def _burst(self) -> bool:
        """Fire `concurrent` PUTs micro-staggered STAGGER_S apart. Stop on first 200.

        The bearer is resolved once per burst so every PUT hits the wire without
        awaiting its own auth check. The stagger spreads the burst across the
        limiter's refill instead of dumping all PUTs into one refused instant.
        """
        headers = await self._claim_headers()
        tasks = [asyncio.create_task(self._single_claim(i + 1, headers, delay=i * self.STAGGER_S))
                 for i in range(self.concurrent)]
        won = False
        for fut in asyncio.as_completed(tasks):
            if await fut == 200:
                won = True
                self._stop.set()
                break
        # cancel any still-in-flight requests once we have a winner
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return won

    # ----- latency lead ----------------------------------------------------

    async def measure_lead(self) -> float:
        """One-way latency to the API (min RTT / 2), for leading the first probe.

        Min of several samples, not mean: queuing delay only ever adds, so the
        fastest sample is closest to the true wire latency. Uses the warm
        session, so the TLS handshake isn't in the measurement.
        """
        best = None
        for _ in range(self.LATENCY_SAMPLES):
            await self.bucket.acquire()
            t0 = time.monotonic()
            try:
                async with self.session.get(self.available_url,
                                            headers=await self._headers()) as resp:
                    await resp.read()
            except aiohttp.ClientError:
                continue
            rtt = time.monotonic() - t0
            best = rtt if best is None else min(best, rtt)
            await asyncio.sleep(0.5)
        if best is None:
            self._log("latency measurement failed — no lead applied")
            return 0.0
        lead = min(best / 2, self.MAX_LEAD_S)
        self._log(f"API RTT {best * 1000:.0f} ms — leading first probe by {lead * 1000:.0f} ms")
        return lead

    # ----- window snipe ----------------------------------------------------

    async def snipe_window(self, window_start: float, window_end: float,
                           poll_interval_ms: int) -> bool:
        """Snipe the drop window; strategy depends on its length.

        Short windows (≤ PUT_PROBE_MAX_WINDOW_S) are probed PUT-first: every
        probe is itself a claim, so the first attempt after the name frees wins
        with zero detection latency. Long windows poll availability with GET
        (cheap on the claim budget) and burst-claim on the first AVAILABLE flip.
        Both paths back off on HTTP 429 and are capped by the shared token
        bucket, so sustained request rate stays bounded either way.
        """
        base = poll_interval_ms / 1000

        # If we're early enough, measure latency and start the loop one-way-trip
        # early so the first probe ARRIVES as the window opens.
        lead = 0.0
        if window_start - self.now() > 10.0:
            lead = await self.measure_lead()

        while self.now() < window_start - lead:
            await asyncio.sleep(min(base, max(0.0, window_start - lead - self.now())))

        if window_end - window_start <= self.PUT_PROBE_MAX_WINDOW_S:
            return await self._snipe_put_first(window_end, base)
        return await self._snipe_poll_burst(window_end, base, poll_interval_ms)

    async def _snipe_put_first(self, window_end: float, base: float) -> bool:
        """Probe with claim PUTs directly: 200 = won, 403 = not free yet."""
        self._log(f"window open — PUT-first probing '{self.target}' every "
                  f"{base * 1000:.0f}ms until window close")
        interval = base
        checks = 0
        while not self._stop.is_set() and self.now() < window_end:
            checks += 1
            code = await self._single_claim(checks, await self._claim_headers())
            if code == 200:
                self.claimed = True
                return True
            if code == 429:
                interval = self._backoff(interval)
                self._log(f"probe #{checks}: backing off to {interval:.1f}s")
            else:  # 403 not free yet / transient error — recover gradually
                interval = max(base, interval / 2)
            await asyncio.sleep(interval)

        self._log("window closed — name not claimed")
        return self.claimed

    async def _snipe_poll_burst(self, window_end: float, base: float,
                                poll_interval_ms: int) -> bool:
        """GET-poll for the flip, then burst rounds of staggered claim PUTs."""
        self._log(f"window open — polling '{self.target}' every {poll_interval_ms}ms "
                  f"until window close")
        interval = base
        checks = 0
        while not self._stop.is_set() and self.now() < window_end:
            status = await self.is_available()
            checks += 1

            if status == "AVAILABLE":
                self._log(f"check #{checks}: '{self.target}' AVAILABLE — firing bursts")
                for round_no in range(self.AGGRESSIVE_ROUNDS):
                    if await self._burst():
                        self.claimed = True
                        return True
                    if self.now() >= window_end:
                        break
                    if round_no < self.AGGRESSIVE_ROUNDS - 1:
                        await asyncio.sleep(self.ROUND_DELAY_S)
                # not won this flip (someone may hold it briefly) — resume polling
                interval = max(base, interval / 2)
            elif status == "RATELIMIT":
                interval = self._backoff(interval)
                self._log(f"check #{checks}: 429 rate limited — backing off to {interval:.1f}s")
            elif status == "TAKEN":
                self._log(f"check #{checks}: '{self.target}' still taken")
                interval = max(base, interval / 2)
            else:  # ERROR
                self._log(f"check #{checks}: availability check error")
                interval = max(base, interval / 2)

            await asyncio.sleep(interval)

        if not self.claimed:
            self._log("window closed — name not claimed")
        return self.claimed
