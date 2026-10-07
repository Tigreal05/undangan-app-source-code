"""Security regression tests for Phase 0 (auth, uploads, XSS)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ADMIN_POST_ROUTES = [
    "/admin/update_homepage",
    "/admin/upload_media",
    "/admin/delete_media",
    "/admin/add_pkg",
    "/admin/delete_pkg",
    "/admin/add_tmpl",
    "/admin/delete_tmpl",
]


# ---------------------------------------------------------------------------
# Admin routes require a session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_get_requires_auth(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.get("/admin", allow_redirects=False)
    assert resp.status in (301, 302, 303, 401)
    if resp.status in (301, 302, 303):
        assert resp.headers["Location"] == "/admin/login"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ADMIN_POST_ROUTES)
async def test_admin_post_routes_require_auth(path, aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.post(path, data={"id": "1"}, allow_redirects=False)
    assert resp.status in (301, 302, 303, 401)
    if resp.status in (301, 302, 303):
        assert resp.headers["Location"] == "/admin/login"


@pytest.mark.asyncio
async def test_admin_post_rejects_tampered_cookie(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    client.session.cookie_jar.update_cookies({"admin_session": "gQ.ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"})
    resp = await client.get("/admin", allow_redirects=False)
    assert resp.status in (301, 302, 303, 401)


# ---------------------------------------------------------------------------
# Login / logout flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_sets_httponly_samesite_cookie(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.post("/admin/login", data={"password": "wrong"}, allow_redirects=False)
    assert resp.status == 401

    resp = await client.post("/admin/login", data={"password": "s3cret-admin-pw"}, allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/admin"

    cookie_header = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header.lower() or "samesite=lax" in cookie_header.lower()

    # Session now works: /admin renders the dashboard.
    resp = await client.get("/admin", allow_redirects=False)
    assert resp.status == 200
    body = await resp.text()
    assert "Panel Kontrol Admin" in body


@pytest.mark.asyncio
async def test_logout_clears_session(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.post("/admin/login", data={"password": "s3cret-admin-pw"}, allow_redirects=False)
    assert resp.status == 302
    resp = await client.get("/admin/logout", allow_redirects=False)
    assert resp.status == 302
    resp = await client.get("/admin", allow_redirects=False)
    assert resp.status in (301, 302, 303)


@pytest.mark.asyncio
async def test_login_rate_limit_5_per_10min(aiohttp_client):
    from app import create_app
    import security

    security.reset_login_attempts()
    client = await aiohttp_client(create_app())

    statuses = []
    for _ in range(6):
        resp = await client.post("/admin/login", data={"password": "bad"}, allow_redirects=False)
        statuses.append(resp.status)
    # first 5 failures -> 401, 6th request blocked by rate limiter -> 429
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429

    # Even the correct password is refused while rate-limited.
    resp = await client.post("/admin/login", data={"password": "s3cret-admin-pw"}, allow_redirects=False)
    assert resp.status == 429
    security.reset_login_attempts()


# ---------------------------------------------------------------------------
# Upload hardening
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_path_traversal_rejected(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.post("/admin/login", data={"password": "s3cret-admin-pw"}, allow_redirects=False)
    assert resp.status == 302

    files = {"file": ("../../x.py", b"print('pwned')", "application/octet-stream")}
    resp = await client.post("/admin/upload_media", data=files, allow_redirects=False)
    assert resp.status == 302
    assert "upload_error" in resp.headers["Location"]

    # Nothing was written outside/inside the upload dir with that name.
    import config
    assert not os.path.exists(os.path.join(config.UPLOAD_DIR, "../../x.py"))
    assert not os.path.exists(os.path.join(config.UPLOAD_DIR, "x.py"))

    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM media_uploads WHERE filename LIKE '%x.py%'")
    assert cur.fetchone()[0] == 0
    conn.close()


@pytest.mark.asyncio
async def test_upload_disallowed_extension_rejected(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.post("/admin/login", data={"password": "s3cret-admin-pw"}, allow_redirects=False)
    assert resp.status == 302

    files = {"file": ("evil.sh", b"#!/bin/sh\nrm -rf /", "application/octet-stream")}
    resp = await client.post("/admin/upload_media", data=files, allow_redirects=False)
    assert "upload_error" in resp.headers["Location"]


@pytest.mark.asyncio
async def test_upload_ok_renames_to_uuid_and_stores_relative(aiohttp_client):
    from app import create_app
    import config
    import uuid as uuid_mod

    client = await aiohttp_client(create_app())
    resp = await client.post("/admin/login", data={"password": "s3cret-admin-pw"}, allow_redirects=False)
    assert resp.status == 302

    files = {"file": ("my photo.PNG", b"\x89PNG fake bytes", "image/png")}
    resp = await client.post("/admin/upload_media", data=files, allow_redirects=False)
    assert resp.headers["Location"] == "/admin"

    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT filename, filepath FROM media_uploads ORDER BY id DESC LIMIT 1")
    filename, filepath = cur.fetchone()
    conn.close()

    stem, ext = filename.rsplit(".", 1)
    assert ext == "png"
    uuid_mod.UUID(stem)  # raises if not a uuid4 hex
    assert os.path.isabs(filepath) is False  # stored relative
    assert ".." not in filepath
    assert os.path.exists(os.path.join(config.UPLOAD_DIR, filename))


@pytest.mark.asyncio
async def test_delete_media_blocks_traversal(aiohttp_client):
    from app import create_app
    import config
    from db import get_conn

    await aiohttp_client(create_app())  # boot once to create schema

    # Seed a DB row whose stored path tries to escape UPLOAD_DIR.
    secret = os.path.abspath("secret_outside.txt")
    with open(secret, "w") as f:
        f.write("do-not-delete")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO media_uploads (filename, filepath) VALUES (?, ?)",
                ("secret_outside.txt", "../secret_outside.txt"))
    conn.commit()
    mid = cur.lastrowid
    conn.close()

    client = await aiohttp_client(create_app())
    resp = await client.post("/admin/login", data={"password": "s3cret-admin-pw"}, allow_redirects=False)
    assert resp.status == 302
    resp = await client.post("/admin/delete_media", data={"id": str(mid)}, allow_redirects=False)
    assert resp.status == 302

    assert os.path.exists(secret), "traversal delete must not remove files outside UPLOAD_DIR"
    os.remove(secret)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM media_uploads WHERE id = ?", (mid,))
    assert cur.fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# XSS: autoescaped rendering
# ---------------------------------------------------------------------------

XSS = "<script>alert(1)</script>"


@pytest.mark.asyncio
async def test_guestbook_escapes_buyer_name(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.get("/guestbook?id=1&buyer_name=" + XSS.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E"))
    assert resp.status == 200
    body = await resp.text()
    assert XSS not in body
    assert "&lt;script&gt;" in body or "\\u003cscript\\u003e" in body


@pytest.mark.asyncio
async def test_guestbook_post_escapes_buyer_name(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.post("/guestbook", data={"id": "1", "buyer_name": XSS})
    body = await resp.text()
    assert XSS not in body
    assert "\\u003cscript\\u003e" in body or "&lt;script&gt;" in body


@pytest.mark.asyncio
async def test_checkout_form_is_post(aiohttp_client):
    from app import create_app

    client = await aiohttp_client(create_app())
    resp = await client.get("/checkout?id=1")
    body = await resp.text()
    # Phase 3.1 checkout contract: form posts to /checkout/confirm
    # (legacy /guestbook target is obsolete).
    assert 'action="/checkout/confirm" method="POST"' in body


@pytest.mark.asyncio
async def test_templates_page_escapes_db_values(aiohttp_client):
    from app import create_app
    from db import get_conn

    await aiohttp_client(create_app())  # boot once to create schema

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO templates (package_id, name, price, discount, duration, image_url, html_code, is_top10) VALUES (?,?,?,?,?,?,?,?)",
                (1, XSS, "Rp 1", XSS, "1 hari", "/x.png", "<html><body>x</body></html>", 0))
    conn.commit()
    conn.close()

    client = await aiohttp_client(create_app())
    for path in ("/templates?package_id=1", "/"):
        resp = await client.get(path)
        body = await resp.text()
        assert XSS not in body, f"unescaped injection on {path}"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM templates WHERE name = ?", (XSS,))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_template_action_escapes(aiohttp_client):
    from app import create_app
    from db import get_conn

    await aiohttp_client(create_app())  # boot once to create schema

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO templates (package_id, name, price, discount, duration, image_url, html_code, is_top10) VALUES (?,?,?,?,?,?,?,?)",
                (1, XSS, "Rp 2", "", "", "/y.png", "<html><body>y</body></html>", 0))
    conn.commit()
    tid = cur.lastrowid
    conn.close()

    client = await aiohttp_client(create_app())
    resp = await client.get(f"/template-action?id={tid}")
    body = await resp.text()
    assert XSS not in body

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM templates WHERE id = ?", (tid,))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_admin_dashboard_escapes(aiohttp_client):
    from app import create_app
    from db import get_conn

    await aiohttp_client(create_app())  # boot once to create schema

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO packages (name, subtitle, image_url) VALUES (?,?,?)",
                (XSS, "sub", "/z.png"))
    pid = cur.lastrowid
    conn.commit()
    conn.close()

    client = await aiohttp_client(create_app())
    resp = await client.post("/admin/login", data={"password": "s3cret-admin-pw"}, allow_redirects=False)
    assert resp.status == 302
    resp = await client.get("/admin")
    body = await resp.text()
    assert XSS not in body

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM packages WHERE id = ?", (pid,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# No hardcoded PIN anywhere
# ---------------------------------------------------------------------------

FORBIDDEN_LITERAL = "1102" + "02"  # legacy admin PIN (split so this file has no literal)


def test_no_hardcoded_pin():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".pytest_cache", "venv")]
        for fn in filenames:
            if not fn.endswith((".py", ".html", ".js", ".txt", ".md")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    if FORBIDDEN_LITERAL in f.read():
                        hits.append(p)
            except (UnicodeDecodeError, OSError):
                pass
    assert hits == [], f"PIN found in: {hits}"
