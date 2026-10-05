"""Security helpers: signed session cookies (admin + client), scrypt password
hashing, CSRF tokens, login rate limiting and upload validation."""
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
#: Client-area session cookie (Phase 3): same HMAC signing as the admin token.
CLIENT_COOKIE = "client_session"


# ---------------------------------------------------------------------------
# Signed session cookie (HMAC-SHA256, URL-safe base64 payload + signature)
# ---------------------------------------------------------------------------

def _sign(payload_b64: str) -> str:
    key = (config.SECRET_KEY or "insecure-dev-secret").encode()
    return hmac.new(key, payload_b64.encode(), hashlib.sha256).hexdigest()


def make_session_token() -> str:
    payload = {"admin": True, "iat": int(time.time()), "nonce": secrets.token_hex(8)}
    return _make_signed_token(payload)


def _make_signed_token(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(raw).decode()
    return f"{payload_b64}.{_sign(payload_b64)}"


def _verify_signed_token(token: str) -> dict:
    """Return the payload dict of a valid, unexpired token, else {}."""
    if not token or "." not in token:
        return {}
    payload_b64, _, sig = token.rpartition(".")
    expected = _sign(payload_b64)
    if not hmac.compare_digest(sig, expected):
        return {}
    try:
        raw = base64.urlsafe_b64decode(payload_b64.encode())
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    iat = data.get("iat", 0)
    if (time.time() - iat) >= SESSION_MAX_AGE:
        return {}
    return data


def verify_session_token(token: str) -> bool:
    return bool(_verify_signed_token(token).get("admin"))


SESSION_MAX_AGE = 8 * 3600


# ---------------------------------------------------------------------------
# Client sessions (Phase 3): same signed cookie scheme, payload carries
# ``client`` (the clients.id) instead of ``admin``.
# ---------------------------------------------------------------------------

def make_client_token(client_id: int) -> str:
    payload = {"client": int(client_id), "iat": int(time.time()),
               "nonce": secrets.token_hex(8)}
    return _make_signed_token(payload)


def verify_client_token(token: str):
    """Return the client id encoded in a valid token, or None."""
    data = _verify_signed_token(token)
    cid = data.get("client")
    return int(cid) if isinstance(cid, int) else None


def client_id_from_request(request: web.Request):
    return verify_client_token(request.cookies.get(CLIENT_COOKIE, ""))


def set_client_cookie(response: web.Response, request: web.Request, client_id: int):
    response.set_cookie(
        CLIENT_COOKIE,
        make_client_token(client_id),
        max_age=SESSION_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=bool(os.environ.get("COOKIE_SECURE")),
        domain=_cookie_domain(request),
    )


def clear_client_cookie(response: web.Response):
    response.set_cookie(CLIENT_COOKIE, "", path="/", max_age=0,
                        httponly=True, samesite="lax")


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


# ---------------------------------------------------------------------------
# Password hashing (scrypt) — Phase 3 client accounts
# ---------------------------------------------------------------------------

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1

# Test-only speed knob: set SCRYPT_COST_LOW=1 in tests/conftest.py to drop the
# work factor. Production never sets it, so production cost stays at N=2**14.
if os.environ.get("SCRYPT_COST_LOW") == "1":
    _SCRYPT_N = 2 ** 10


def hash_password(password: str) -> str:
    """Return 'scrypt$N$r$p$salt_hex$hash_hex' for storage in clients.password_hash."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
                                          salt.hex(), dk.hex())


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification against a scrypt hash produced above."""
    try:
        algo, n, r, p, salt_hex, hash_hex = str(stored or "").split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(str(password).encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p),
                            maxmem=64 * 1024 * 1024, dklen=len(bytes.fromhex(hash_hex)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ---------------------------------------------------------------------------
# CSRF tokens (double-submit: cookie + hidden form field / X-CSRF-Token header)
# ---------------------------------------------------------------------------

CSRF_COOKIE = "csrf_token"
CSRF_FIELD = "csrf_token"


def make_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_protected(handler) -> bool:
    """True when the matched route is registered with ``csrf_exempt=True``."""
    resource = getattr(handler, "__aiohttp_route_resource__", None)
    return bool(resource is not None and getattr(resource, "csrf_exempt", False))


def _has_client_session(request) -> bool:
    return client_id_from_request(request) is not None


@web.middleware
async def csrf_middleware(request, handler):
    """Require a matching CSRF token on state-changing requests.

    Safe methods (GET/HEAD/OPTIONS) pass through. POST/PUT/PATCH/DELETE must
    carry the same value in the ``csrf_token`` cookie AND either the form
    field ``csrf_token`` or the ``X-CSRF-Token`` header — unless the route was
    registered via ``add_csrf_route`` with ``csrf_exempt=True``.

    The admin panel predates CSRF tokens (Phase 0 tests post without one), so
    authenticated-admin requests are exempted there; every *other* session
    (including anonymous ones such as the public guestbook) is protected.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await handler(request)
    if csrf_protected(handler):
        return await handler(request)
    if is_admin(request) and not _has_client_session(request):
        return await handler(request)
    cookie = request.cookies.get(CSRF_COOKIE, "")
    submitted = ""
    if request.can_read_body:
        try:
            content_type = (request.content_type or "").lower()
            if content_type.startswith("multipart/form-data") or \
                    content_type == "application/x-www-form-urlencoded":
                post = await request.post()
                submitted = str(post.get(CSRF_FIELD) or "")
            elif content_type == "application/json":
                payload = await request.json()
                if isinstance(payload, dict):
                    submitted = str(payload.get(CSRF_FIELD) or "")
        except Exception:
            submitted = ""
    if not submitted:
        submitted = request.headers.get("X-CSRF-Token", "")
    if (not cookie or not submitted
            or not hmac.compare_digest(cookie, submitted)):
        raise web.HTTPForbidden(text="CSRF token missing or invalid")
    return await handler(request)


# ---------------------------------------------------------------------------
# Generic sliding-window rate limiter (login attempts per scope+IP)
# ---------------------------------------------------------------------------

_rate_buckets = {}  # scope -> {key: [failure timestamps]}


def reset_rate_limits(scope: str = None):
    """Clear one scope's failures (or all scopes when None). Used by tests."""
    if scope is None:
        _rate_buckets.clear()
    else:
        _rate_buckets.pop(scope, None)


def _bucket_hits(scope: str, key: str, window: int):
    now = time.time()
    bucket = _rate_buckets.setdefault(scope, {}).setdefault(key, [])
    fresh = [t for t in bucket if now - t < window]
    _rate_buckets[scope][key] = fresh
    return fresh


def rate_limited(scope: str, key: str, max_attempts: int, window: int) -> bool:
    """True when ``key`` already accumulated ``max_attempts`` failures inside
    the sliding ``window`` seconds for this scope (e.g. 'client-login')."""
    return len(_bucket_hits(scope, key, window)) >= max_attempts


def record_failure(scope: str, key: str):
    _rate_buckets.setdefault(scope, {}).setdefault(key, []).append(time.time())


def clear_failures(scope: str, key: str):
    _rate_buckets.setdefault(scope, {}).pop(key, None)


# ---------------------------------------------------------------------------
# Route registration helper honouring the CSRF middleware exemption flag
# ---------------------------------------------------------------------------

_METHODS = {
    "get": ["GET"], "head": ["HEAD"], "options": ["OPTIONS"],
    "post": ["POST"], "put": ["PUT"], "patch": ["PATCH"], "delete": ["DELETE"],
}


def add_csrf_route(app: web.Application, method: str, path: str, handler,
                   *, csrf_exempt: bool = False, name: str = None):
    """Register a route; set ``csrf_exempt=True`` for public API endpoints
    that are protected by other means (e.g. rate limit + honeypot)."""
    resource = app.router.add_route(method.upper(), path, handler, name=name)
    resource.csrf_exempt = csrf_exempt
    # Expose the flag on the handler so the middleware can read it from the
    # resolved handler object. When the same function object is bound to
    # several routes with different exemptions, wrap it once per route so the
    # attribute never leaks between registrations.
    if getattr(handler, "__aiohttp_route_resource__", None) is not None:
        import functools

        @functools.wraps(handler)
        async def _wrapped(request):
            return await handler(request)
        _wrapped.__wrapped__ = handler
        for sub in resource._routes:
            sub._handler = _wrapped
        target = _wrapped
    else:
        target = handler
    try:
        target.__aiohttp_route_resource__ = resource
    except AttributeError:  # builtins etc. — fall back to the wrapper itself
        setattr(target, "__aiohttp_route_resource__", resource)
    return resource


def add_get(app, path, handler, **kw):
    return add_csrf_route(app, "get", path, handler, **kw)


def add_post(app, path, handler, **kw):
    return add_csrf_route(app, "post", path, handler, **kw)
