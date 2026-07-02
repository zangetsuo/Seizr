"""Microsoft -> Xbox Live -> XSTS -> Minecraft authentication chain.

Implements the full device-code OAuth2 flow (no password, browser based),
caches the refresh token (file for CLI, callback for the multi-user web
server), and exposes a bearer token that auto-refreshes before expiry.

Run standalone to test the chain end to end:

    python -m seizr.auth
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp

from seizr import ROOT_DIR

# Public MSA app client id (PrismLauncher's) — device-code flow enabled and
# pre-consented for the XboxLive.signin scope. Override in config.json via
# "client_id" if you register your own Azure app.
DEFAULT_CLIENT_ID = "c36a9fb6-4f2a-41ff-90bd-ae7cc92031eb"

DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

SCOPE = "XboxLive.signin offline_access"
# Honor AUTH_CACHE (set in container/cloud to a mounted volume) so the refresh
# token survives restarts; default to a file in the repo root locally.
CACHE_FILE = Path(os.environ.get("AUTH_CACHE", ROOT_DIR / ".auth_cache.json"))

# Refresh the Minecraft bearer this many seconds before it actually expires.
REFRESH_MARGIN = 60


class AuthError(Exception):
    """Raised when any step of the auth chain fails."""


class MinecraftAuth:
    """Holds the auth state and produces a valid Minecraft bearer token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str = DEFAULT_CLIENT_ID,
        cache_file: Path = CACHE_FILE,
        refresh_token: Optional[str] = None,
        on_refresh: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        """`on_refresh(token)` (async) makes this a per-user instance: the token
        is injected and saved via the callback instead of the local file cache
        (used by the multi-user web server). Without it, the file cache is used
        (CLI / single-user)."""
        self.session = session
        self.client_id = client_id
        self.cache_file = cache_file
        self._on_refresh = on_refresh
        self._use_file = on_refresh is None

        self.refresh_token: Optional[str] = refresh_token
        self.bearer_token: Optional[str] = None
        self.bearer_expires_at: float = 0.0  # epoch seconds

        self._lock = asyncio.Lock()

    # ----- token persistence -----------------------------------------------

    def _load_cache(self) -> None:
        if not self._use_file:
            return
        if self.cache_file.exists():
            try:
                data = json.loads(self.cache_file.read_text())
                self.refresh_token = data.get("refresh_token")
            except (json.JSONDecodeError, OSError):
                self.refresh_token = None

    async def _persist(self) -> None:
        if self._use_file:
            try:
                self.cache_file.write_text(json.dumps({"refresh_token": self.refresh_token}))
                self.cache_file.chmod(0o600)
            except OSError:
                pass
        elif self._on_refresh and self.refresh_token:
            await self._on_refresh(self.refresh_token)

    def forget(self) -> None:
        """Drop all tokens (used when a user disconnects their MS account)."""
        self.refresh_token = None
        self.bearer_token = None
        self.bearer_expires_at = 0.0

    # ----- Microsoft OAuth2 ------------------------------------------------

    async def begin_device_code(self) -> dict:
        """Request a device code. Returns the dict with user_code/verification_uri.

        Used by both the CLI flow and the web UI (which shows the code to the
        user and then calls poll_device_token() in the background).
        """
        async with self.session.post(
            DEVICE_CODE_URL,
            data={"client_id": self.client_id, "scope": SCOPE},
        ) as resp:
            if resp.status != 200:
                raise AuthError(f"device code request failed: {resp.status} {await resp.text()}")
            return await resp.json()

    async def poll_device_token(self, dc: dict) -> dict:
        """Poll the token endpoint until the user finishes the browser sign-in."""
        interval = dc.get("interval", 5)
        deadline = time.time() + dc.get("expires_in", 900)

        while time.time() < deadline:
            await asyncio.sleep(interval)
            async with self.session.post(
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id,
                    "device_code": dc["device_code"],
                },
            ) as resp:
                payload = await resp.json()
            if resp.status == 200:
                return payload
            err = payload.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            raise AuthError(f"device code login failed: {err} {payload.get('error_description')}")

        raise AuthError("device code expired before sign-in completed")

    async def _device_code_login(self) -> dict:
        """CLI device-code flow: print the code, then block until sign-in done."""
        dc = await self.begin_device_code()
        print("\n=== Microsoft sign-in ===")
        print(f"Open: {dc['verification_uri']}")
        print(f"Enter code: {dc['user_code']}")
        print("Waiting for you to finish in the browser...\n")
        return await self.poll_device_token(dc)

    async def _refresh_oauth(self) -> dict:
        """Exchange the cached refresh token for a fresh MS access token."""
        async with self.session.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "scope": SCOPE,
                "refresh_token": self.refresh_token,
            },
        ) as resp:
            payload = await resp.json()
        if resp.status != 200:
            raise AuthError(f"refresh token rejected: {payload.get('error_description')}")
        return payload

    # ----- Xbox / XSTS / Minecraft ----------------------------------------

    async def _xbl_auth(self, ms_access_token: str) -> tuple[str, str]:
        body = {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={ms_access_token}",
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        }
        async with self.session.post(XBL_URL, json=body) as resp:
            if resp.status != 200:
                raise AuthError(f"Xbox Live auth failed: {resp.status} {await resp.text()}")
            data = await resp.json()
        token = data["Token"]
        uhs = data["DisplayClaims"]["xui"][0]["uhs"]
        return token, uhs

    async def _xsts_auth(self, xbl_token: str) -> str:
        body = {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        }
        async with self.session.post(XSTS_URL, json=body) as resp:
            if resp.status == 401:
                data = await resp.json()
                xerr = data.get("XErr")
                raise AuthError(f"XSTS denied (XErr={xerr}). 2148916233=no Xbox account, "
                                "2148916238=child account.")
            if resp.status != 200:
                raise AuthError(f"XSTS auth failed: {resp.status} {await resp.text()}")
            data = await resp.json()
        return data["Token"]

    async def _minecraft_login(self, uhs: str, xsts_token: str) -> dict:
        body = {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}
        async with self.session.post(MC_LOGIN_URL, json=body) as resp:
            if resp.status != 200:
                raise AuthError(f"Minecraft login failed: {resp.status} {await resp.text()}")
            return await resp.json()

    # ----- public API ------------------------------------------------------

    async def login(self) -> None:
        """Ensure we have a valid bearer token, using cache when possible."""
        async with self._lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        """Full login: cached refresh token first, device code as fallback.
        Caller must hold self._lock."""
        self._load_cache()
        if self.refresh_token:
            try:
                await self._run_chain(use_refresh=True)
                return
            except AuthError as exc:
                print(f"Cached login failed ({exc}); falling back to device code.")
        await self._run_chain(use_refresh=False)

    async def refresh_login(self) -> None:
        """Silent re-auth from the stored refresh token. Raises AuthError if
        the token is missing/rejected — no device-code fallback."""
        async with self._lock:
            await self._run_chain(use_refresh=True)

    async def _run_chain(self, use_refresh: bool) -> None:
        if use_refresh:
            oauth = await self._refresh_oauth()
        else:
            oauth = await self._device_code_login()
        await self.finish_oauth(oauth)

    async def finish_oauth(self, oauth: dict) -> None:
        """Given an MS OAuth token response, run Xbox -> XSTS -> Minecraft."""
        self.refresh_token = oauth["refresh_token"]
        await self._persist()

        xbl_token, uhs = await self._xbl_auth(oauth["access_token"])
        xsts_token = await self._xsts_auth(xbl_token)
        mc = await self._minecraft_login(uhs, xsts_token)

        self.bearer_token = mc["access_token"]
        self.bearer_expires_at = time.time() + mc.get("expires_in", 86400)
        print("Minecraft bearer token acquired.")

    def _bearer_fresh(self) -> bool:
        return bool(self.bearer_token) and time.time() < self.bearer_expires_at - REFRESH_MARGIN

    async def get_bearer(self) -> str:
        """Return a valid bearer, refreshing it if near expiry.

        Fast path is lock-free; the locked slow path re-checks so concurrent
        callers (poll loop + burst) never run duplicate refresh chains.
        """
        token = self.bearer_token
        if token and self._bearer_fresh():
            return token
        async with self._lock:
            token = self.bearer_token
            if token and self._bearer_fresh():
                return token
            if self.bearer_token or self.refresh_token:
                print("Bearer near expiry; refreshing...")
                await self._run_chain(use_refresh=True)
            else:
                await self._login_locked()
        if self.bearer_token is None:
            raise AuthError("auth chain completed without a bearer token")
        return self.bearer_token

    async def get_profile(self) -> Optional[dict]:
        """Fetch the account's Minecraft profile (None if no profile yet)."""
        bearer = await self.get_bearer()
        async with self.session.get(
            MC_PROFILE_URL, headers={"Authorization": f"Bearer {bearer}"}
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 404:
                return None
            raise AuthError(f"profile fetch failed: {resp.status} {await resp.text()}")


async def _main() -> None:
    async with aiohttp.ClientSession() as session:
        auth = MinecraftAuth(session)
        await auth.login()
        bearer = await auth.get_bearer()
        print(f"\nBearer (first 24 chars): {bearer[:24]}...")
        print(f"Expires in: {int(auth.bearer_expires_at - time.time())}s")

        profile = await auth.get_profile()
        if profile:
            print(f"Logged in as: {profile['name']} ({profile['id']})")
        else:
            print("Account has no Minecraft profile (no name set yet).")


if __name__ == "__main__":
    asyncio.run(_main())
