"""Password hashing and login sessions (standard library only).

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes ("pbkdf2_sha256$iterations$salt$hash").
Session cookies hold a random token; only its SHA-256 is stored in infra.auth_session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

ITERATIONS = 120_000
SESSION_HOURS = 12


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
