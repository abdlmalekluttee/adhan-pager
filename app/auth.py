"""Authentication: local user store, bcrypt hashing, signed session cookies,
login rate-limiting. Single-admin by default; data lives in the persistent
config volume so it survives container rebuilds.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger("auth")

USERS_PATH = os.environ.get("USERS_PATH") or os.path.join(
    os.path.dirname(os.environ.get("CONFIG_PATH", "/config/config.yaml")), "users.json"
)

SESSION_COOKIE = "ap_session"
CSRF_COOKIE = "ap_csrf"
DEFAULT_MAX_AGE = 8 * 3600          # 8 hours
REMEMBER_MAX_AGE = 7 * 24 * 3600    # 7 days

USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
MIN_PASSWORD_LEN = 8

# login rate limiting
_MAX_FAILS = 5
_LOCK_SECONDS = 5 * 60
_attempts: dict[str, list] = {}     # key -> [fail_count, lock_until]
_lock = threading.RLock()
_store_lock = threading.RLock()

_DATA: dict | None = None
_serializer: URLSafeTimedSerializer | None = None


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _now() -> int:
    return int(time.time())


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _verify(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except Exception:  # noqa: BLE001
        return False


def _default_user(username: str = "admin") -> dict:
    return {
        "username": username,
        "password_hash": _hash("admin"),
        "must_change_password": True,
        "language": "en",
        "theme": "auto",
        "accent_color": "default",
        "token_version": 1,
        "last_login": None,
    }


class AuthStoreError(RuntimeError):
    """users.json exists but is unreadable/corrupt — refuse to fall back to defaults."""


def _load() -> dict:
    global _DATA, _serializer
    if _DATA is not None:
        return _DATA
    with _store_lock:
        if _DATA is not None:
            return _DATA
        data = None
        if os.path.exists(USERS_PATH):
            # File is present — it MUST be readable and valid. Never silently fall
            # back to admin/admin, or a permission/corruption issue would reopen the
            # system with default credentials.
            try:
                with open(USERS_PATH, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as e:  # noqa: BLE001
                raise AuthStoreError(
                    f"{USERS_PATH} exists but is invalid ({e}). Refusing to start with "
                    "default credentials. Fix the file, or delete it manually to "
                    "intentionally reset login to admin/admin.") from e
            if not isinstance(data, dict) or not isinstance(data.get("users"), dict) \
                    or not data["users"]:
                raise AuthStoreError(
                    f"{USERS_PATH} exists but has no valid users. Refusing to start with "
                    "default credentials. Fix the file, or delete it manually to reset.")
            if not data.get("secret"):
                data["secret"] = secrets.token_hex(32)
                _DATA = data
                _save_locked()
        else:
            # No file at all → first run: create the default admin (must change pw).
            data = {"secret": secrets.token_hex(32), "users": {"admin": _default_user()}}
            _DATA = data
            _save_locked()
            log.info("auth: created default admin account (must change password)")
        _DATA = data
        _serializer = URLSafeTimedSerializer(data["secret"], salt="ap-session")
        return _DATA


def _save_locked():
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)
    tmp = USERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_DATA, fh, indent=2)
    os.replace(tmp, USERS_PATH)


def _save():
    with _store_lock:
        _save_locked()


def _users() -> dict:
    return _load()["users"]


def _ser() -> URLSafeTimedSerializer:
    _load()
    return _serializer


# --------------------------------------------------------------------------- #
# Public profile (no secrets)
# --------------------------------------------------------------------------- #
def public_profile(user: dict) -> dict:
    return {
        "username": user["username"],
        "must_change_password": bool(user.get("must_change_password")),
        "language": user.get("language", "en"),
        "theme": user.get("theme", "auto"),
        "accent_color": user.get("accent_color", "default"),
        "last_login": user.get("last_login"),
    }


def get_user(username: str) -> dict | None:
    return _users().get(username)


def store_error() -> str | None:
    """Return a message if users.json is present-but-broken, else None. Never raises."""
    try:
        _load()
        return None
    except AuthStoreError as e:
        return str(e)


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def _rl_key(username: str, ip: str) -> str:
    return f"{(username or '').lower()}|{ip or '-'}"


def is_locked(username: str, ip: str) -> int:
    """Return seconds remaining on lockout, or 0."""
    with _lock:
        rec = _attempts.get(_rl_key(username, ip))
        if not rec:
            return 0
        remaining = rec[1] - _now()
        return remaining if remaining > 0 else 0


def record_fail(username: str, ip: str):
    with _lock:
        key = _rl_key(username, ip)
        rec = _attempts.get(key, [0, 0])
        rec[0] += 1
        if rec[0] >= _MAX_FAILS:
            rec[1] = _now() + _LOCK_SECONDS
            rec[0] = 0
        _attempts[key] = rec


def reset_fails(username: str, ip: str):
    with _lock:
        _attempts.pop(_rl_key(username, ip), None)


# --------------------------------------------------------------------------- #
# Auth core
# --------------------------------------------------------------------------- #
def authenticate(username: str, password: str) -> dict | None:
    user = _users().get(username)
    if not user:
        return None
    if not _verify(password, user["password_hash"]):
        return None
    return user


def issue_token(user: dict) -> str:
    return _ser().dumps({"u": user["username"], "v": user.get("token_version", 1)})


def verify_token(token: str, max_age: int = REMEMBER_MAX_AGE) -> dict | None:
    if not token:
        return None
    try:
        data = _ser().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return None
    user = _users().get(data.get("u", ""))
    if not user:
        return None
    if int(data.get("v", -1)) != int(user.get("token_version", 1)):
        return None  # invalidated (password changed / logout-all)
    return user


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def mark_login(user: dict):
    with _store_lock:
        user["last_login"] = _now()
        _save_locked()


def logout_user(user: dict):
    """Invalidate all existing tokens for this user by bumping the token version."""
    with _store_lock:
        user["token_version"] = int(user.get("token_version", 1)) + 1
        _save_locked()


# Mark cookies Secure when deployed behind HTTPS (AUTH_COOKIE_SECURE=true|1|yes).
COOKIE_SECURE = os.environ.get("AUTH_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #
def validate_username(username: str) -> str | None:
    if not username or not username.strip():
        return "Username is required."
    if username != username.strip():
        return "Username cannot start or end with spaces."
    if not USERNAME_RE.match(username):
        return "Username must be 3–32 chars: letters, numbers, dot, dash, underscore."
    return None


def validate_password(password: str, username: str) -> str | None:
    if not password:
        return "New password is required."
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if password.lower() == (username or "").lower():
        return "Password must not be the same as the username."
    if password == "admin":
        return "The default password is not allowed."
    return None


def password_strength(password: str) -> str:
    score = 0
    if len(password) >= 12:
        score += 1
    if re.search(r"[a-z]", password) and re.search(r"[A-Z]", password):
        score += 1
    if re.search(r"\d", password):
        score += 1
    if re.search(r"[^a-zA-Z0-9]", password):
        score += 1
    return ["weak", "weak", "fair", "good", "strong"][score]


def change_password(user: dict, current: str, new_password: str) -> str | None:
    if not _verify(current, user["password_hash"]):
        return "Current password is incorrect."
    err = validate_password(new_password, user["username"])
    if err:
        return err
    with _store_lock:
        user["password_hash"] = _hash(new_password)
        user["must_change_password"] = False
        user["token_version"] = int(user.get("token_version", 1)) + 1  # invalidate others
        _save_locked()
    return None


def update_account(user: dict, new_username: str | None, prefs: dict | None) -> str | None:
    users = _users()
    with _store_lock:
        if new_username and new_username != user["username"]:
            err = validate_username(new_username)
            if err:
                return err
            if new_username in users:
                return "That username is already taken."
            old = user["username"]
            user["username"] = new_username
            users[new_username] = user
            users.pop(old, None)
            user["token_version"] = int(user.get("token_version", 1)) + 1
        if prefs:
            if prefs.get("language") in ("en", "ar"):
                user["language"] = prefs["language"]
            if prefs.get("theme") in ("auto", "dark", "light"):
                user["theme"] = prefs["theme"]
            if isinstance(prefs.get("accent_color"), str) and len(prefs["accent_color"]) <= 20:
                user["accent_color"] = prefs["accent_color"]
        _save_locked()
    return None


def save_preferences(user: dict, prefs: dict):
    with _store_lock:
        if prefs.get("language") in ("en", "ar"):
            user["language"] = prefs["language"]
        if prefs.get("theme") in ("auto", "dark", "light"):
            user["theme"] = prefs["theme"]
        if isinstance(prefs.get("accent_color"), str) and len(prefs["accent_color"]) <= 20:
            user["accent_color"] = prefs["accent_color"]
        _save_locked()
