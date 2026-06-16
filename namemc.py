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
_NAME_OK = re.compile(r"^[A-Za-z0-9_]{1,16}$")


class DropLookupError(Exception):
    """Lookup failed (bad name, Cloudflare block, no window, etc.)."""


async def fetch_drop_window(name: str) -> dict:
    """Return {"start": iso, "end": iso|None} for `name`, or raise.

    Imports Playwright lazily so the rest of the app runs without it installed.
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
    m = _WINDOW_RE.search(html)
    if not m:
        # Distinguish "page loaded but no window" from a Cloudflare block.
        if "Just a moment" in html or len(html) < 2000:
            raise DropLookupError("Blocked by Cloudflare — try again shortly.")
        raise DropLookupError("No drop window listed for this name.")
    return {"start": m.group(1), "end": m.group(2)}


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
