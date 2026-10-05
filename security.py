"""Security helpers: signed admin session cookie, login rate limiting, upload validation."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from aiohttp import web

import config

SESSION_COOKIE = "admin_session"


# ---------------------------------------------------------------------------
# Signed session cookie (HMAC-SHA256, URL-safe base64 payload + signature)
# ---------------------------------------------------------------------------

def _sign(payload_b64: str) -> str:
    key = (config.SECRET_KEY or "insecure-dev-secret").encode()
    return hmac.new(key, payload_b64.encode(), hashlib.sha256).hexdigest()


def make_session_token() -> str:
    payload = {"admin": True, "iat": int(time.time()), "nonce": secrets.token_hex(8)}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(raw).decode()
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_session_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    payload_b64, _, sig = token.rpartition(".")
    expected = _sign(payload_b64)
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        raw = base64.urlsafe_b64decode(payload_b64.encode())
        data = json.loads(raw)
    except Exception:
        return False
    if not data.get("admin"):
        return False
    iat = data.get("iat", 0)
    return (time.time() - iat) < SESSION_MAX_AGE


SESSION_MAX_AGE = 8 * 3600


def _cookie_domain(request: web.Request):
    """Return the cookie Domain attribute (None -> host-only cookie).

    Only scopes the cookie to the registered base domain when the request is
    actually served on a subdomain of BASE_DOMAIN (e.g. an invitation page);
    plain hosts like localhost / 127.0.0.1 keep a host-only cookie.
    """
    try:
        host = (request.url.hostname or "").lower()
    except Exception:
        host = ""
    base = (config.BASE_DOMAIN or "").lower().lstrip(".")
    if base and host.endswith("." + base) and host != base:
        return "." + base
    return None


def set_session_cookie(response: web.Response, request: web.Request):
    """Set a signed, HttpOnly, SameSite=Lax session cookie."""
    response.set_cookie(
        SESSION_COOKIE,
        make_session_token(),
        max_age=SESSION_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=bool(os.environ.get("COOKIE_SECURE")),
        domain=_cookie_domain(request),
    )


def clear_session_cookie(response: web.Response):
    response.set_cookie(SESSION_COOKIE, "", path="/", max_age=0,
                        httponly=True, samesite="lax")


def is_admin(request: web.Request) -> bool:
    return verify_session_token(request.cookies.get(SESSION_COOKIE, ""))


# ---------------------------------------------------------------------------
# Login rate limiting: 5 failures / 10 minutes / IP
# ---------------------------------------------------------------------------

_login_failures = {}  # ip -> list of failure timestamps


def reset_login_attempts():
    """Clear the failure store (used by tests)."""
    _login_failures.clear()


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote or "unknown"


def is_rate_limited(request: web.Request) -> bool:
    ip = _client_ip(request)
    now = time.time()
    attempts = [t for t in _login_failures.get(ip, [])
                if now - t < config.LOGIN_WINDOW_SECONDS]
    _login_failures[ip] = attempts
    return len(attempts) >= config.LOGIN_MAX_ATTEMPTS


def record_login_failure(request: web.Request):
    ip = _client_ip(request)
    _login_failures.setdefault(ip, []).append(time.time())


def clear_login_failures(request: web.Request):
    _login_failures.pop(_client_ip(request), None)


# ---------------------------------------------------------------------------
# Admin auth middleware: protect GET /admin and ALL /admin/* POST routes
# ---------------------------------------------------------------------------

@web.middleware
def _requires_admin(path: str, method: str) -> bool:
    """True when the route must carry a valid admin session cookie."""
    if path == "/admin":
        return True
    if path.startswith("/admin/"):
        # The only public admin endpoints are the login form/action itself.
        # Everything else under /admin/* — GET pages and ALL POST actions
        # (including /admin/logout so it can only clear an active session) —
        # requires authentication.
        return path != "/admin/login"
    return False


@web.middleware
async def admin_auth_middleware(request, handler):
    path = request.path
    needs_auth = _requires_admin(path, request.method)
    if needs_auth and not is_admin(request):
        raise web.HTTPFound("/admin/login")
    return await handler(request)


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------

_SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]+$")


def validate_upload(filename: str, size: int):
    """Return (ok, reason, extension). Enforces allowlist + max size."""
    if not filename:
        return False, "Empty filename", None
    # Take only the final path component and reject traversal attempts.
    base = os.path.basename(filename.replace("\\", "/"))
    if base != filename.replace("\\", "/") or ".." in filename:
        return False, "Invalid filename", None
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if not _SAFE_EXT_RE.match(ext) or ext not in config.ALLOWED_EXTENSIONS:
        return False, "File extension not allowed", ext
    limit = config.MAX_VIDEO_UPLOAD_SIZE if ext == "mp4" else config.MAX_UPLOAD_SIZE
    if size > limit:
        return False, "File too large", ext
    return True, None, ext


def safe_upload_path(rel_path: str):
    """Resolve a stored relative path inside UPLOAD_DIR, blocking traversal.

    Returns the absolute path, or None if it escapes UPLOAD_DIR.
    """
    root = os.path.realpath(config.UPLOAD_DIR)
    candidate = os.path.realpath(os.path.join(root, rel_path))
    if candidate == root or candidate.startswith(root + os.sep):
        return candidate
    return None
