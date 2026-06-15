"""Minecraft name sniper — entry point and orchestration.

Pipeline:
  1. Load config.json
  2. Sync the clock against NTP (correct for system drift)
  3. Authenticate (cached refresh token -> bearer)
  4. Confirm the account has no active name-change cooldown
  5. Resolve drop time (manual ISO string, or auto-detect = last change + 37d)
  6. Countdown -> poll availability from T-3min -> burst at T-burst_start
  7. Report the claimed profile

Usage:
    python sniper.py            # uses config.json
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import ntplib

from api import NameSniper
from auth import MinecraftAuth

CONFIG_FILE = Path(__file__).with_name("config.json")

POLL_LEAD_SECONDS = 180          # start polling availability 3 min before drop
GIVE_UP_AFTER_SECONDS = 60       # keep bursting this long past drop, then stop
NAME_DROP_DAYS = 37              # availability = last change + 37 days
ASHCON_URL = "https://api.ashcon.app/mojang/v2/user/{name}"


class Clock:
    """NTP-corrected clock. now() returns true epoch seconds despite system drift."""

    def __init__(self, offset: float = 0.0):
        self.offset = offset

    def now(self) -> float:
        return time.time() + self.offset


async def sync_clock(ntp_server: str) -> Clock:
    loop = asyncio.get_running_loop()

    def _query() -> float:
        client = ntplib.NTPClient()
        resp = client.request(ntp_server, version=3, timeout=5)
        return resp.offset

    try:
        offset = await loop.run_in_executor(None, _query)
        print(f"NTP sync OK ({ntp_server}): system clock off by {offset * 1000:.0f} ms")
        return Clock(offset)
    except Exception as exc:  # ntplib raises bare exceptions on timeout
        print(f"NTP sync failed ({exc}); using system clock uncorrected.")
        return Clock(0.0)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit("config.json not found.")
    cfg = json.loads(CONFIG_FILE.read_text())
    if not cfg.get("target_name"):
        sys.exit("config.json: 'target_name' is required.")
    return cfg


def parse_drop_time(value: str) -> float:
    """ISO 8601 -> epoch seconds. Assumes UTC if no tz given."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def auto_detect_drop(session: aiohttp.ClientSession, name: str) -> float:
    """Last name change + 37 days, via a third-party history source."""
    async with session.get(ASHCON_URL.format(name=name)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"history lookup failed: HTTP {resp.status}")
        data = await resp.json()
    history = data.get("username_history") or []
    changes = [h for h in history if h.get("changed_at")]
    if not changes:
        raise RuntimeError("no change date available — set 'drop_time' manually")
    last = max(parse_drop_time(h["changed_at"]) for h in changes)
    drop = last + NAME_DROP_DAYS * 86400
    print(f"Auto-detected last change + {NAME_DROP_DAYS}d -> "
          f"{datetime.fromtimestamp(drop, tz=timezone.utc).isoformat()}")
    return drop


def fmt_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def countdown_to(clock: Clock, drop: float, until: float, label: str) -> None:
    """Print a live countdown until clock reaches `until` (epoch seconds)."""
    while clock.now() < until:
        remaining = drop - clock.now()
        print(f"\r{label} | drop in {fmt_remaining(remaining)}", end="", flush=True)
        await asyncio.sleep(1)
    print()


async def run() -> None:
    cfg = load_config()
    target = cfg["target_name"]
    client_id = cfg.get("client_id") or None

    clock = await sync_clock(cfg.get("ntp_server", "pool.ntp.org"))

    async with aiohttp.ClientSession() as session:
        auth = MinecraftAuth(session, client_id=client_id) if client_id else MinecraftAuth(session)
        await auth.login()

        sniper = NameSniper(
            session, auth, target, clock.now,
            concurrent_requests=cfg.get("concurrent_requests", 20),
        )

        if not await sniper.check_no_cooldown():
            sys.exit("Account has an active name-change cooldown. Aborting.")

        # resolve drop time
        if cfg.get("drop_time"):
            drop = parse_drop_time(cfg["drop_time"])
            print(f"Drop time (manual): {datetime.fromtimestamp(drop, tz=timezone.utc).isoformat()}")
        else:
            print("No drop_time set — auto-detecting from name history...")
            drop = await auto_detect_drop(session, target)

        now = clock.now()
        if drop <= now:
            print("Drop time already passed — attempting immediate claim.")
        else:
            # wait until T-3min, refreshing the bearer along the way
            poll_start = drop - POLL_LEAD_SECONDS
            if now < poll_start:
                await countdown_to(clock, drop, poll_start, f"Waiting to poll '{target}'")
                await auth.get_bearer()  # refresh if near expiry before the action window

        burst_at = drop - cfg.get("burst_start_seconds", 2)
        poll_deadline = max(clock.now(), burst_at)

        print(f"Polling '{target}' every {cfg.get('poll_interval_ms', 500)}ms "
              f"until T-{cfg.get('burst_start_seconds', 2)}s ...")
        await sniper.poll_until_available(cfg.get("poll_interval_ms", 500), poll_deadline)

        # hold until the exact burst moment if availability flipped early
        while clock.now() < burst_at:
            await asyncio.sleep(0.05)

        claimed = await sniper.claim_burst(give_up_at=drop + GIVE_UP_AFTER_SECONDS)

        if claimed:
            profile = await auth.get_profile()
            name = profile["name"] if profile else target
            print(f"\n*** SUCCESS — profile name is now: {name} ***")
        else:
            print("\nName was not claimed.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted.")
