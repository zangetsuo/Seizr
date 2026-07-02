"""Seizr account routes: register/login/logout, email verification, Google OAuth."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from seizr import accounts
from seizr.accounts import AccountError
from seizr.db import Database
from seizr.mailer import send_verification
from seizr.web.deps import (
    GSTATE_COOKIE,
    SECURE_COOKIES,
    SESSION_COOKIE,
    current_user,
    set_session_cookie,
)

router = APIRouter()


@router.get("/api/me")
async def me(request: Request):
    user = await current_user(request)
    if not user:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "email": user["email"],
        "display_name": user["display_name"],
        "email_verified": bool(user["email_verified"]),
    }


@router.post("/auth/register")
async def register(request: Request):
    db: Database = request.app.state.db
    data = await request.json()
    try:
        user_id, token = await accounts.register_local(
            db, data.get("email", ""), data.get("password", "")
        )
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    link = f"{str(request.base_url).rstrip('/')}/auth/verify?token={token}"
    try:
        await send_verification(data["email"].strip().lower(), link)
    except Exception as exc:  # account exists; don't fail signup on a mail hiccup
        print(f"WARNING: verification email failed to send: {exc}")
    return {"ok": True, "message": "Check your email to verify your account."}


@router.get("/auth/verify")
async def verify(request: Request, token: str = ""):
    db: Database = request.app.state.db
    user_id = await db.consume_email_token(token, "verify")
    if not user_id:
        return RedirectResponse("/login?error=verify", status_code=303)
    await db.set_email_verified(user_id)
    sess = await accounts.start_session(db, user_id)
    resp = RedirectResponse("/app", status_code=303)
    set_session_cookie(resp, sess)
    return resp


@router.post("/auth/login")
async def auth_login(request: Request):
    db: Database = request.app.state.db
    data = await request.json()
    user = await accounts.login_local(db, data.get("email", ""), data.get("password", ""))
    if not user:
        return JSONResponse({"error": "Invalid email or password."}, status_code=401)
    if not user["email_verified"]:
        return JSONResponse({"error": "Verify your email first.", "code": "unverified"},
                            status_code=403)
    sess = await accounts.start_session(db, user["id"])
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, sess)
    return resp


@router.post("/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await request.app.state.db.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/auth/google")
async def google_start(request: Request):
    if not accounts.google_enabled():
        return RedirectResponse("/login?error=google_off", status_code=303)
    redirect_uri = f"{str(request.base_url).rstrip('/')}/auth/google/callback"
    state = accounts.new_token()
    resp = RedirectResponse(accounts.google_auth_url(redirect_uri, state), status_code=303)
    resp.set_cookie(GSTATE_COOKIE, state, max_age=600, httponly=True,
                    samesite="lax", secure=SECURE_COOKIES, path="/")
    return resp


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    db: Database = request.app.state.db
    if not code or not state or state != request.cookies.get(GSTATE_COOKIE):
        return RedirectResponse("/login?error=google", status_code=303)
    redirect_uri = f"{str(request.base_url).rstrip('/')}/auth/google/callback"
    try:
        info = await accounts.google_exchange(request.app.state.session, code, redirect_uri)
        user = await accounts.google_upsert_user(db, info)
    except AccountError:
        return RedirectResponse("/login?error=google", status_code=303)
    sess = await accounts.start_session(db, user["id"])
    resp = RedirectResponse("/app", status_code=303)
    set_session_cookie(resp, sess)
    resp.delete_cookie(GSTATE_COOKIE, path="/")
    return resp
