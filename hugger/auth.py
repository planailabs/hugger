"""Authentication: password hashing, API tokens, login throttling, CSRF.

- Web UI: argon2-hashed password -> signed session cookie.
- Extension/API: constant-time Bearer token check.
Built for internet exposure, so login is rate-limited and all comparisons are
constant-time.
"""
from __future__ import annotations

import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import cfg

_ph = PasswordHasher()

# --- passwords -----------------------------------------------------------

def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def set_password(plain: str) -> None:
    cfg.set_password_hash(hash_password(plain))


def verify_password(plain: str) -> bool:
    h = cfg.password_hash
    if not h:
        return False
    try:
        _ph.verify(h, plain)
    except VerifyMismatchError:
        return False
    if _ph.check_needs_rehash(h):  # transparently upgrade params over time
        cfg.set_password_hash(_ph.hash(plain))
    return True


# --- API token (extension) ----------------------------------------------

def verify_token(token: str | None) -> bool:
    if not token:
        return False
    return secrets.compare_digest(token, cfg.api_token)


def rotate_token() -> str:
    tok = secrets.token_urlsafe(32)
    cfg.set_api_token(tok)
    return tok


def bearer_from_request(req) -> str | None:
    h = req.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    # Also accept the extension's custom header for environments that strip Authorization.
    return req.headers.get("x-hugger-token") or None


# --- login throttle ------------------------------------------------------
# ponytail: in-memory per-IP throttle, single-process. Swap for Redis/slowapi
# if you run multiple workers.
_MAX_FAILS = 5
_LOCK_SECONDS = 300
_attempts: dict[str, tuple[int, float]] = {}


def is_locked(ip: str) -> float:
    """Return seconds remaining on lockout, or 0 if not locked."""
    fails, until = _attempts.get(ip, (0, 0.0))
    if fails >= _MAX_FAILS and time.monotonic() < until:
        return round(until - time.monotonic())
    return 0


def record_failure(ip: str) -> None:
    fails, _ = _attempts.get(ip, (0, 0.0))
    fails += 1
    _attempts[ip] = (fails, time.monotonic() + _LOCK_SECONDS)


def record_success(ip: str) -> None:
    _attempts.pop(ip, None)


# --- CSRF (double-submit via session token) ------------------------------

def csrf_token(sess) -> str:
    tok = sess.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        sess["csrf"] = tok
    return tok


def csrf_ok(sess, value: str | None) -> bool:
    expected = sess.get("csrf")
    return bool(expected and value and secrets.compare_digest(expected, value))
