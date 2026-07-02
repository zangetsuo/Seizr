"""At-rest encryption for sensitive secrets (Minecraft refresh tokens).

Key is derived from SEIZR_SECRET_KEY. Set a strong value in production — if it
changes, previously stored tokens can no longer be decrypted (users just
re-link their Microsoft account). A fixed dev key is used if unset so local
runs survive restarts, with a warning.
"""

import base64
import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_secret = os.environ.get("SEIZR_SECRET_KEY")
if not _secret:
    print("WARNING: SEIZR_SECRET_KEY not set — using an insecure dev key. "
          "Set it in production.")
    _secret = "dev-insecure-key-change-me"

_key = base64.urlsafe_b64encode(hashlib.sha256(_secret.encode()).digest())
_fernet = Fernet(_key)


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> Optional[str]:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
