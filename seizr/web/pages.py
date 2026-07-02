"""Static page routes (landing, login, register, app shell)."""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse

from seizr import ROOT_DIR
from seizr.web.deps import current_user

STATIC_DIR = ROOT_DIR / "static"

router = APIRouter()


@router.get("/")
async def landing():
    return FileResponse(STATIC_DIR / "landing.html")


@router.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@router.get("/register")
async def register_page():
    return FileResponse(STATIC_DIR / "register.html")


@router.get("/app")
async def webapp_page(request: Request):
    if not await current_user(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "app.html")
