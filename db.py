"""SQLite data layer for Seizr accounts.

One file-based database (zero-ops, runs anywhere — including the Oracle free
tier). Holds users, login sessions, and short-lived email tokens. A single
shared aiosqlite connection serializes access; WAL mode keeps reads concurrent.

Migrating to Postgres later means swapping this module — the rest of the app
talks to it through the async methods here, not raw SQL.
"""

import os
import time
from pathlib import Path
from typing import Optional

import aiosqlite

import crypto

DB_PATH = Path(os.environ.get("SEIZR_DB", Path(__file__).with_name("seizr.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE,
    password_hash  TEXT,                       -- NULL for Google-only accounts
    google_sub     TEXT    UNIQUE,             -- Google subject id, NULL otherwise
    display_name   TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    created_at      REAL   NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS email_tokens (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose    TEXT    NOT NULL,               -- e.g. "verify"
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS minecraft_accounts (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    refresh_token TEXT,                         -- encrypted (crypto.encrypt)
    profile_name  TEXT,
    updated_at    REAL    NOT NULL
);
"""


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def connect(cls, path: Path = DB_PATH) -> "Database":
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # ----- users -----------------------------------------------------------

    async def create_user(
        self,
        email: str,
        password_hash: Optional[str] = None,
        google_sub: Optional[str] = None,
        display_name: Optional[str] = None,
        email_verified: bool = False,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO users (email, password_hash, google_sub, display_name,
                                  email_verified, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (email.lower(), password_hash, google_sub, display_name,
             int(email_verified), time.time()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_user_by_email(self, email: str) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),))
        return await cur.fetchone()

    async def get_user_by_google_sub(self, sub: str) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM users WHERE google_sub = ?", (sub,))
        return await cur.fetchone()

    async def get_user(self, user_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return await cur.fetchone()

    async def set_email_verified(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))
        await self.conn.commit()

    async def link_google_sub(self, user_id: int, sub: str) -> None:
        await self.conn.execute("UPDATE users SET google_sub = ? WHERE id = ?", (sub, user_id))
        await self.conn.commit()

    # ----- sessions --------------------------------------------------------

    async def create_session(self, token: str, user_id: int, ttl_seconds: int) -> None:
        now = time.time()
        await self.conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + ttl_seconds),
        )
        await self.conn.commit()

    async def get_session_user(self, token: str) -> Optional[aiosqlite.Row]:
        """Return the user row for a live session, or None if missing/expired."""
        cur = await self.conn.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, time.time()),
        )
        return await cur.fetchone()

    async def delete_session(self, token: str) -> None:
        await self.conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await self.conn.commit()

    # ----- email tokens ----------------------------------------------------

    async def create_email_token(self, token: str, user_id: int, purpose: str,
                                  ttl_seconds: int) -> None:
        now = time.time()
        await self.conn.execute(
            """INSERT INTO email_tokens (token, user_id, purpose, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token, user_id, purpose, now, now + ttl_seconds),
        )
        await self.conn.commit()

    # ----- minecraft accounts (per-user MS token) --------------------------

    async def get_mc_refresh(self, user_id: int) -> Optional[str]:
        """Return the decrypted Minecraft refresh token for a user, or None."""
        cur = await self.conn.execute(
            "SELECT refresh_token FROM minecraft_accounts WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row or not row["refresh_token"]:
            return None
        return crypto.decrypt(row["refresh_token"])

    async def save_mc_refresh(self, user_id: int, token: str) -> None:
        await self.conn.execute(
            """INSERT INTO minecraft_accounts (user_id, refresh_token, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET refresh_token = excluded.refresh_token,
                                                  updated_at = excluded.updated_at""",
            (user_id, crypto.encrypt(token), time.time()),
        )
        await self.conn.commit()

    async def save_mc_profile(self, user_id: int, profile_name: str) -> None:
        await self.conn.execute(
            """INSERT INTO minecraft_accounts (user_id, profile_name, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET profile_name = excluded.profile_name,
                                                  updated_at = excluded.updated_at""",
            (user_id, profile_name, time.time()),
        )
        await self.conn.commit()

    async def clear_mc(self, user_id: int) -> None:
        """Forget a user's connected Minecraft account (token + profile)."""
        await self.conn.execute(
            "DELETE FROM minecraft_accounts WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def get_mc(self, user_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM minecraft_accounts WHERE user_id = ?", (user_id,))
        return await cur.fetchone()

    async def consume_email_token(self, token: str, purpose: str) -> Optional[int]:
        """Return user_id and delete the token if valid+unexpired, else None."""
        cur = await self.conn.execute(
            "SELECT user_id, expires_at FROM email_tokens WHERE token = ? AND purpose = ?",
            (token, purpose),
        )
        row = await cur.fetchone()
        if not row:
            return None
        await self.conn.execute("DELETE FROM email_tokens WHERE token = ?", (token,))
        await self.conn.commit()
        if row["expires_at"] < time.time():
            return None
        return row["user_id"]
