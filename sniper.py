"""Minecraft name sniper — entry point and orchestration.

Pipeline:
  1. Load config.json
  2. Sync the clock against NTP (correct for system drift)
  3. Authenticate (cached refresh token -> bearer)
  4. Confirm the account has no active name-change cooldown
  5. Resolve the drop window (window_start .. window_end, manual ISO strings)
  6. Wait for the window, then poll availability across it and burst-claim on
     the first AVAILABLE flip
  7. Report the claimed profile

Names no longer drop at an exact instant — Mojang releases them somewhere inside
a window — so we poll the whole range rather than firing at a fixed T-2s.

Usage:
    python sniper.py            # uses config.json
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import ntplib

from api import NameSniper
from auth import MinecraftAuth

CONFIG_FILE = Path(__file__).with_name("config.json")

# Default window length when window_end is left blank.
DEFAULT_WINDOW_MINUTES = 15


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


def resolve_window(cfg: dict) -> tuple[float, float]:
    """Return (window_start, window_end) epoch seconds from config."""
    if not cfg.get("window_start"):
        sys.exit("config.json: 'window_start' is required (ISO 8601, UTC).")
    start = parse_drop_time(cfg["window_start"])
    if cfg.get("window_end"):
        end = parse_drop_time(cfg["window_end"])
    else:
        end = start + DEFAULT_WINDOW_MINUTES * 60
    if end <= start:
        sys.exit("config.json: 'window_end' must be after 'window_start'.")
    return start, end


async def run() -> None:
    cfg = load_config()
    target = cfg["target_name"]
    client_id = cfg.get("client_id") or None
    win_start, win_end = resolve_window(cfg)

    clock = await sync_clock(cfg.get("ntp_server", "pool.ntp.org"))

    async with aiohttp.ClientSession() as session:
        auth = MinecraftAuth(session, client_id=client_id) if client_id else MinecraftAuth(session)
        await auth.login()

        sniper = NameSniper(
            session, auth, target, clock.now,
            concurrent_requests=cfg.get("concurrent_requests", 5),
        )

        if not await sniper.check_no_cooldown():
            sys.exit("Account has an active name-change cooldown. Aborting.")

        iso = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
        print(f"Drop window: {iso(win_start)}  ->  {iso(win_end)}")

        # wait for the window to open, refreshing the bearer along the way
        if clock.now() < win_start:
            await countdown_to(clock, win_start, win_start, f"Waiting for window on '{target}'")
            await auth.get_bearer()

        claimed = await sniper.snipe_window(
            win_start, win_end, cfg.get("poll_interval_ms", 1000)
        )

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
