"""FastAPI app assembly.

Run locally:
    .venv/bin/python -m uvicorn webapp:app --port 8000
    # then open http://localhost:8000

In a container / cloud VM:
    uvicorn webapp:app --host 0.0.0.0 --port 8000

The frontend lives in static/ and talks to this server: log in (Microsoft
device code), enter the target + drop window, then run with a live log +
countdown streamed over SSE.
"""

import contextlib

import aiohttp
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from seizr.db import Database
from seizr.web import account_routes, mc_routes, pages, snipe_routes
from seizr.web.pages import STATIC_DIR


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Long keepalive + DNS cache: the claim burst reuses warm TLS connections
    # instead of paying a handshake at the moment that decides the race.
    connector = aiohttp.TCPConnector(ttl_dns_cache=300, keepalive_timeout=60)
    app.state.session = aiohttp.ClientSession(connector=connector)
    app.state.db = await Database.connect()
    app.state.runtimes = {}  # user_id -> UserRuntime
    yield
    await app.state.db.close()
    await app.state.session.close()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(account_routes.router)
    app.include_router(mc_routes.router)
    app.include_router(snipe_routes.router)
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
