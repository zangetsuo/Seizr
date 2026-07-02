"""NameMC drop-window lookup.

NameMC publishes an estimated *drop window* (start + end) for names that are
freeing up. There is no official API and the site sits behind Cloudflare, so we
drive a headless Chromium (Playwright) with light anti-automation tweaks to read
the page and scrape the "Drop Window" times.

This is best-effort: Cloudflare may change its challenge, datacenter IPs are
often blocked outright, and the times are NameMC's *estimate* — Seizr still polls
the whole window. The web UI only calls this when the user asks for it.
"""
from __future__ import annotations

import re

# The page renders client-side, so we wait for the Cloudflare interstitial
# ("Just a moment...") to clear before reading content.
_NAME_URL = "https://namemc.com/name/{name}"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
# "Drop Window" label followed by one or two <time datetime="..."> elements.
_WINDOW_RE = re.compile(
    r"Drop Window.*?<time[^>]*datetime=\"([^\"]+)\""
    r"(?:.*?<time[^>]*datetime=\"([^\"]+)\")?",
    re.IGNORECASE | re.DOTALL,
)
# NameMC's <meta name="description" content="Status: <X>, Searches: ...">
_STATUS_RE = re.compile(r"name=\"description\" content=\"Status:\s*([^,\"]+)", re.IGNORECASE)
_NAME_OK = re.compile(r"^[A-Za-z0-9_]{1,16}$")


class DropLookupError(Exception):
    """Lookup failed (bad name, Cloudflare block, not dropping, etc.)."""


async def fetch_drop_info(name: str) -> dict:
    """Look up a name's snipe-ability on NameMC.

    Returns {"status", "available", "start", "end"}:
      - status:    NameMC's status string ("Available", "Possibly Available",
                   "Locked", "Unavailable", ...).
      - available: True if claimable right now (status contains "Available" —
                   covers both "Available" and "Possibly Available").
      - start/end: the Drop Window ISO times (UTC), or None if no window listed.

    Raises DropLookupError only when the name is *not* snipeable (taken / locked
    with no upcoming window) or the lookup itself failed. Imports Playwright
    lazily so the rest of the app runs without it installed.
    """
    if not _NAME_OK.match(name or ""):
        raise DropLookupError("Invalid Minecraft name.")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # optional dependency
        raise DropLookupError(
            "NameMC lookup needs Playwright. Install: pip install playwright "
            "&& python -m playwright install chromium"
        ) from exc

    html = await _load_page(async_playwright, _NAME_URL.format(name=name))
    if "Just a moment" in html or len(html) < 2000:
        raise DropLookupError("Blocked by Cloudflare — try again shortly.")

    sm = _STATUS_RE.search(html)
    status = sm.group(1).strip() if sm else "Unknown"
    # Exact match — "Unavailable" contains "available" as a substring.
    available = status.lower() in ("available", "possibly available")

    wm = _WINDOW_RE.search(html)
    start = wm.group(1) if wm else None
    end = wm.group(2) if wm else None

    # Snipeable if it's free now or has an upcoming/active drop window.
    if not available and not start:
        raise DropLookupError(f"'{name}' is not dropping (status: {status}).")

    return {"status": status, "available": available, "start": start, "end": end}


async def _load_page(async_playwright, url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            ctx = await browser.new_context(
                user_agent=_UA, viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            # Poll until the Cloudflare interstitial title clears (or give up).
            for _ in range(10):
                await page.wait_for_timeout(1500)
                if "moment" not in (await page.title()).lower():
                    break
            return await page.content()
        finally:
            await browser.close()
