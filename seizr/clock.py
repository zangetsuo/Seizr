"""NTP-corrected clock and drop-window time helpers.

Names no longer drop at an exact instant — Mojang releases them somewhere
inside a window — so everything downstream works with (window_start,
window_end) epoch seconds measured against real time, not system time.
"""

import asyncio
import time
from datetime import datetime, timezone

import ntplib

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


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
