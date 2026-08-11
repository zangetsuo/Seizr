"""Terminal entry point — snipe a name from config.json.

Pipeline:
  1. Load config.json
  2. Sync the clock against NTP (correct for system drift)
  3. Authenticate (cached refresh token -> bearer)
  4. Confirm the account has no active name-change cooldown
  5. Resolve the drop window (window_start .. window_end, manual ISO strings)
  6. Wait for the window, then poll availability across it and burst-claim on
     the first AVAILABLE flip
  7. Report the claimed profile

Usage:
    python sniper.py            # uses config.json
"""

import asyncio
import json
import sys

import aiohttp

from seizr import ROOT_DIR
from seizr.auth import MinecraftAuth
from seizr.clock import (
    Clock,
    DEFAULT_WINDOW_MINUTES,
    fmt_remaining,
    iso_utc,
    parse_drop_time,
    sync_clock,
)
from seizr.mojang import NameSniper

CONFIG_FILE = ROOT_DIR / "config.json"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit("config.json not found.")
    cfg = json.loads(CONFIG_FILE.read_text())
    if not cfg.get("target_name"):
        sys.exit("config.json: 'target_name' is required.")
    return cfg


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
    win_start, win_end = resolve_window(cfg)

    clock = await sync_clock(cfg.get("ntp_server", "pool.ntp.org"))

    connector = aiohttp.TCPConnector(ttl_dns_cache=300, keepalive_timeout=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        auth = MinecraftAuth(session, client_id=client_id) if client_id else MinecraftAuth(session)
        await auth.login()

        sniper = NameSniper(
            session, auth, target, clock.now,
            concurrent_requests=cfg.get("concurrent_requests", 5),
        )

        if not await sniper.check_no_cooldown():
            sys.exit("Account has an active name-change cooldown. Aborting.")

        print(f"Drop window: {iso_utc(win_start)}  ->  {iso_utc(win_end)}")

        # wait for the window to open, refreshing the bearer along the way;
        # hand off 15 s early so the sniper can measure latency lead
        if clock.now() < win_start - 15:
            await countdown_to(clock, win_start, win_start - 15,
                               f"Waiting for window on '{target}'")
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


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
