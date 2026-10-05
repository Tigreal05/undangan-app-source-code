"""Admin routes: login/logout, dashboard and all /admin/* POST actions.

Auth is enforced by security.admin_auth_middleware (registered in app.py),
which protects GET /admin and every /admin/* POST route. The only public
admin endpoints are /admin/login and /admin/logout.
"""
import hashlib
import os
import secrets
import uuid

from aiohttp import web
from markupsafe import Markup

import config
import security
from db import get_conn
from routes.common import render
from services import tiers as tiers_svc


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login - SUKA MOTO</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: sans-serif; }
        body { background: #09090b; color: #f4f4f5; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
        .login-box { width: 100%; max-width: 340px; background: #121215; border: 1px solid #27272a; border-radius: 12px; padding: 24px; }
        h1 { font-size: 16px; margin-bottom: 4px; color: #fbbf24; }
        p.sub { font-size: 11px; color: #a1a1aa; margin-bottom: 16px; }
        input[type=password] { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #3f3f46; background: #18181b; color: #fff; font-size: 13px; margin: 6px 0; }
        button { width: 100%; background: #fbbf24; color: #000; font-weight: bold; border: none; border-radius: 8px; padding: 10px; margin-top: 10px; cursor: pointer; font-size: 13px; }
        .err { color: #ef4444; font-size: 11px; margin-top: 10px; }
        a.back { display: inline-block; margin-top: 14px; color: #fbbf24; font-size: 11px; text-decoration: none; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>Panel Admin</h1>
        <p class="sub">Masukkan kata sandi admin untuk melanjutkan.</p>
        <form action="/admin/login" method="POST">
            <input type="password" name="password" placeholder="Kata Sandi" required autofocus>
            <button type="submit">Masuk</button>
        </form>
        {% if error %}<div class="err">{{ error }}</div>{% endif %}
        <a class="back" href="/">&larr; Kembali ke Beranda</a>
    </div>
</body>
</html>
"""


async def handle_login_form(request):
    html = render(LOGIN_HTML, error=None)
    return web.Response(text=html, content_type='text/html')


async def handle_login(request):
    data = await request.post()
    password = data.get('password', '') or ''

    if security.is_rate_limited(request):
        html = render(LOGIN_HTML, error="Terlalu banyak percobaan gagal. Coba lagi dalam 10 menit.")
        return web.Response(text=html, status=429, content_type='text/html')

    expected = config.ADMIN_PASSWORD
    ok = bool(expected) and hmac_equals(password, expected)
    if ok:
        security.clear_login_failures(request)
        response = web.HTTPFound('/admin')
        security.set_session_cookie(response, request)
        return response

    security.record_login_failure(request)
    html = render(LOGIN_HTML, error="Kata sandi salah.")
    return web.Response(text=html, status=401, content_type='text/html')


def hmac_equals(a: str, b: str) -> bool:
    """Constant-time comparison of salted digests."""
    ha = hashlib.sha256(("lm:" + a).encode()).digest()
    hb = hashlib.sha256(("lm:" + b).encode()).digest()
    return secrets.compare_digest(ha, hb)


async def handle_logout(request):
    response = web.HTTPFound('/admin/login')
    security.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Dashboard (autoescaped Jinja2 template)
# ---------------------------------------------------------------------------

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Dashboard - SUKA MOTO</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: sans-serif; }
        body { background: #09090b; color: #f4f4f5; padding: 16px; }
        .wrap { max-width: 600px; margin: 0 auto; background: #121215; padding: 20px; border-radius: 12px; border: 1px solid #27272a; }
        input, select, textarea, button { padding: 10px 12px; margin: 6px 0; border-radius: 8px; border: 1px solid #3f3f46; background: #18181b; color: #fff; width: 100%; font-size: 13px; }
        textarea { resize: vertical; height: 100px; font-family: monospace; font-size: 11px; }
        .btn-save { background: #fbbf24; color: #000; font-weight: bold; cursor: pointer; border: none; margin-top: 10px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 12px; }
        th { text-align: left; padding: 10px; border-bottom: 2px solid #3f3f46; color: #fbbf24; }
        .section-box { background: #18181b; border: 1px solid #27272a; padding: 15px; border-radius: 10px; margin-bottom: 20px; }
    </style>
</head>
<body>
    <div class="wrap">
        <h2 style="font-size: 16px; margin-bottom: 5px;">Panel Kontrol Admin</h2>
        <p style="font-size:11px; color:#a1a1aa; margin-bottom:20px;">
            <a href="/" style="color:#fbbf24; text-decoration:none;">&larr; Kembali ke Beranda</a>
            &nbsp;|&nbsp;
            <a href="/admin/logout" style="color:#ef4444; text-decoration:none;">Keluar</a>
        </p>

        <div class="section-box">
            <h3 style="font-size:13px; margin-bottom:8px; color:#fbbf24;">Edit File Fisik Halaman Beranda (homepage.html)</h3>
            <form action="/admin/update_homepage" method="POST">
                <label style="font-size: 11px; color: #a1a1aa; display:block; margin-top:4px;">Isi file homepage.html:</label>
                <textarea name="homepage_html" required style="height:120px;">{{ current_homepage_html }}</textarea>
                <button type="submit" class="btn-save">Simpan ke File homepage.html</button>
            </form>
        </div>

        <div class="section-box">
            <h3 style="font-size:13px; margin-bottom:8px; color:#fbbf24;">File Manager (Asset Hosting)</h3>
            <form action="/admin/upload_media" method="POST" enctype="multipart/form-data">
                <label style="font-size: 11px; color: #a1a1aa; display:block;">Upload File / Gambar Aset:</label>
                <input type="file" name="file" required style="background:#121215; padding:6px;">
                <button type="submit" class="btn-save">Upload & Dapatkan Link</button>
            </form>
            {% if upload_error %}<p style="font-size:11px; color:#ef4444; margin-top:8px;">{{ upload_error }}</p>{% endif %}
            <h4 style="font-size:11px; margin-top:15px; color:#a1a1aa; margin-bottom:5px;">Daya Simpan Aset (Copy Link):</h4>
            <table>
                <tr><th>Nama File / Link Aset</th><th style="text-align:right;">Aksi</th></tr>
                {{ media_rows }}
            </table>
        </div>

        <div class="section-box">
            <h3 style="font-size:13px; margin-bottom:8px; color:#fbbf24;">Tambah Paket Utama</h3>
            <form action="/admin/add_pkg" method="POST">
                <input type="text" name="name" placeholder="Nama Paket (Contoh: Platinum VIP)" required>
                <input type="text" name="subtitle" placeholder="Subjudul (Contoh: All-in-One Exclusive)" required>
                <input type="text" name="image_url" placeholder="URL Gambar Cover Card" value="https://images.unsplash.com/photo-1519741497674-611481863552?auto=format&fit=crop&w=600&q=80" required>
                <button type="submit" class="btn-save">Simpan Paket</button>
            </form>
            <h4 style="font-size:11px; margin-top:15px; color:#a1a1aa; margin-bottom:5px;">Daftar Paket:</h4>
            <table>
                <tr><th>Paket</th><th>Keterangan</th><th>Aksi</th></tr>
                {{ pkg_rows }}
            </table>
        </div>

        <div class="section-box">
            <h3 style="font-size:13px; margin-bottom:8px; color:#fbbf24;">Tambah Template & Kodingan HTML</h3>
            <form action="/admin/add_tmpl" method="POST">
                <select name="package_id" required>
                    <option value="">-- Pilih Kategori Paket --</option>
                    {{ pkg_options }}
                </select>
                <select name="tier_id" required>
                    <option value="">-- Pilih Tier (durasi aktif) --</option>
                    {{ tier_options }}
                </select>
                <input type="text" name="name" placeholder="Nama Template (Contoh: Royal Velvet)" required>
                <input type="text" name="price" placeholder="Harga (Contoh: Rp 250.000)" required>
                <input type="text" name="discount" placeholder="Keterangan Diskon (Cth: Diskon 20%)">
                <div style="display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 12px; color: #fff;">
                    <input type="checkbox" name="is_top10" value="1" style="width: auto; margin:0;"> Masukkan ke 10 Template Terbaik (Homepage)
                </div>
                <input type="text" name="image_url" placeholder="URL Gambar Preview Template" value="https://images.unsplash.com/photo-1520854221256-17451cc331bf?auto=format&fit=crop&w=400&q=80" required>
                <label style="font-size: 11px; color: #a1a1aa; display:block; margin-top:8px;">Source Code HTML Template:</label>
                <textarea name="html_code" placeholder="<!DOCTYPE html>... Paste kodingan HTML undangan web di sini ..." required></textarea>
                <button type="submit" class="btn-save">Simpan Template & Kodingan</button>
            </form>
            <h4 style="font-size:11px; margin-top:15px; color:#a1a1aa; margin-bottom:5px;">Daftar Template:</h4>
            <table>
                <tr><th>Template / Paket</th><th>Harga</th><th>Aksi</th></tr>
                {{ tmpl_rows }}
            </table>
        </div>
    </div>
    <script>
        document.querySelectorAll('.auto-full-url').forEach(input => {
            input.value = window.location.origin + input.getAttribute('data-url');
        });
    </script>
</body>
</html>
"""

MEDIA_ROW_HTML = """
        <tr style="border-bottom:1px solid #27272a;">
            <td style="padding:8px; word-break:break-all;">
                <a href="{{ full_url }}" target="_blank" style="color:#34d399; text-decoration:none; display:block; margin-bottom:4px;">{{ mname }}</a>
                <input type="text" readonly value="" class="auto-full-url" data-url="{{ full_url }}" onclick="this.select();" style="font-size:10px; padding:4px 6px; background:#121215; border:1px solid #3f3f46; color:#fbbf24; border-radius:4px; width:100%;">
            </td>
            <td style="padding:8px; text-align:right; white-space:nowrap; vertical-align:top;">
                <button type="button" onclick="navigator.clipboard.writeText(this.closest('tr').querySelector('.auto-full-url').value).then(() => { alert('Link aset berhasil disalin!'); });" style="background:#27272a; color:#fbbf24; border:none; padding:4px 8px; border-radius:4px; font-size:9px; cursor:pointer; font-weight:bold;">Copy</button>
                <form action="/admin/delete_media" method="POST" style="display:inline;">
                    <input type="hidden" name="id" value="{{ mid }}">
                    <button type="submit" style="background:#ef4444; color:#fff; border:none; padding:4px 8px; border-radius:4px; font-size:9px; cursor:pointer; margin-left:4px;">Hapus</button>
                </form>
            </td>
        </tr>
"""


async def handle_admin(request):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, subtitle FROM packages')
    pkgs = cursor.fetchall()

    cursor.execute('''
        SELECT t.id, t.name, t.price, p.name, t.discount, t.duration, t.is_top10, ti.code
        FROM templates t
        JOIN packages p ON t.package_id = p.id
        LEFT JOIN tiers ti ON t.tier_id = ti.id
    ''')
    tmpls = cursor.fetchall()

    cursor.execute('SELECT id, filename, filepath FROM media_uploads ORDER BY id DESC')
    media_files = cursor.fetchall()
    conn.close()

    if os.path.exists(config.HOMEPAGE_FILE):
        with open(config.HOMEPAGE_FILE, "r", encoding="utf-8") as f:
            current_homepage_html = f.read()
    else:
        current_homepage_html = ""

    upload_error = request.query.get('upload_error', '')

    pkg_options = ""
    for pid, pname, _ in pkgs:
        pkg_options += render('<option value="{{ pid }}">{{ pname }}</option>', pid=pid, pname=pname)

    cursor = get_conn()  # reopen briefly for tier list (init guarantees table)
    tier_rows_db = cursor.execute(
        'SELECT id, code, name, active_days FROM tiers ORDER BY sort_order'
    ).fetchall()
    cursor.close()

    tier_options = ""
    for tid_, tcode, tname_, tdays in tier_rows_db:
        tier_options += render(
            '<option value="{{ tid_ }}">{{ tname_ }} — aktif {{ tdays }} hari</option>',
            tid_=tid_, tname_=tname_, tdays=tdays)

    pkg_rows = ""
    for pid, name, sub in pkgs:
        pkg_rows += render("""
        <tr style="border-bottom:1px solid #27272a;">
            <td style="padding:10px; font-weight:bold;">{{ name }}</td>
            <td style="padding:10px; color:#a1a1aa;">{{ sub }}</td>
            <td style="padding:10px;">
                <form action="/admin/delete_pkg" method="POST" style="display:inline;">
                    <input type="hidden" name="id" value="{{ pid }}">
                    <button type="submit" style="background:#ef4444; color:#fff; border:none; padding:4px 8px; border-radius:4px; cursor:pointer; font-size:10px;">Hapus</button>
                </form>
            </td>
        </tr>
        """, pid=pid, name=name, sub=sub)

    tmpl_rows = ""
    for tid, tname, tprice, pname, tdisc, tdur, top10, ttier in tmpls:
        # Duration always comes from the tier's active_days (never templates.duration).
        tdur = f"{tiers_svc.active_days(ttier)} hari" if ttier else "30 hari"
        tmpl_rows += render("""
        <tr style="border-bottom:1px solid #27272a;">
            <td style="padding:10px; font-weight:bold;">{{ tname }}
                {% if tdisc or tdur or top10 %}<br><span style='font-size:9px; color:#fbbf24;'>{{ tdisc }} | {{ tdur }}{% if top10 %} <span style='background:#34d399; color:#000; padding:1px 4px; border-radius:3px; font-size:8px; font-weight:bold;'>Top 10</span>{% endif %}</span>{% endif %}
                <br><span style="font-size:9px; color:#71717a;">Paket: {{ pname }}</span>
            </td>
            <td style="padding:10px; color:#34d399; font-weight:bold;">{{ tprice }}</td>
            <td style="padding:10px;">
                <form action="/admin/delete_tmpl" method="POST" style="display:inline;">
                    <input type="hidden" name="id" value="{{ tid }}">
                    <button type="submit" style="background:#ef4444; color:#fff; border:none; padding:4px 8px; border-radius:4px; cursor:pointer; font-size:10px;">Hapus</button>
                </form>
            </td>
        </tr>
        """, tid=tid, tname=tname, tprice=tprice, pname=pname, tdisc=tdisc, tdur=tdur, top10=top10)

    media_rows = ""
    for mid, mname, mpath in media_files:
        full_url = f"/static_uploads/{mname}"
        media_rows += render(MEDIA_ROW_HTML, mid=mid, mname=mname, full_url=full_url)

    if not media_rows:
        media_rows = "<tr><td colspan='2' style='text-align:center; color:#71717a; padding:10px; font-size:11px;'>Belum ada file di-upload.</td></tr>"

    admin_html = render(
        ADMIN_HTML,
        current_homepage_html=current_homepage_html,
        upload_error=upload_error,
        media_rows=Markup(media_rows),
        pkg_options=Markup(pkg_options),
        tier_options=Markup(tier_options),
        pkg_rows=Markup(pkg_rows),
        tmpl_rows=Markup(tmpl_rows),
    )
    return web.Response(text=admin_html, content_type='text/html')


# ---------------------------------------------------------------------------
# POST actions (all protected by the admin auth middleware)
# ---------------------------------------------------------------------------

async def handle_update_homepage(request):
    data = await request.post()
    new_html = data.get('homepage_html', '')
    with open(config.HOMEPAGE_FILE, "w", encoding="utf-8") as f:
        f.write(new_html)
    raise web.HTTPFound('/admin')


def _stream_limited(field, limit: int):
    """Yield chunks of an upload field, raising web.HTTPFound when over the limit."""
    async def read():
        chunks = []
        size = 0
        while True:
            chunk = await field.read_chunk(8192, raise_exception=False)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                return None, size
            chunks.append(chunk)
        return chunks, size
    return read()


async def _read_multipart_file(reader):
    """Find the 'file' part; return (filename, chunks, size) — size may exceed
    the hard cap temporarily, it is re-checked against the tiered limits."""
    field = await reader.next()
    while field is not None and (field.name != "file" or not field.filename):
        field = await reader.next()
    if field is None or not field.filename:
        return None, None, 0
    # Generous streaming guard: never buffer more than 2x the largest allowed
    # payload; the precise per-type limit is enforced after validation.
    hard_cap = max(config.MAX_UPLOAD_SIZE, config.MAX_VIDEO_UPLOAD_SIZE) * 2
    chunks = []
    size = 0
    while True:
        chunk = await field.read_chunk(8192, raise_exception=False)
        if not chunk:
            break
        size += len(chunk)
        if size > hard_cap:
            break
        chunks.append(chunk)
    return field.filename, chunks, size


async def handle_upload_media(request):
    ct = (request.content_type or '').lower()
    if ct.startswith('multipart/'):
        reader = await request.multipart()
        original_name, chunks, size = await _read_multipart_file(reader)
    elif request.has_body:
        # Non-multipart POST (urlencoded probe / simple client): accept a
        # plain 'file' field holding just a filename so validation still runs.
        post = await request.post()
        raw = post.get('file', '')
        if isinstance(raw, str):
            original_name, chunks, size = (raw or None), b'', 0
        else:  # aiohttp FileField from an urlencoded-style upload
            original_name = raw.filename
            body = raw.file.read()
            chunks, size = [body], len(body)
    else:
        original_name, chunks, size = None, None, 0

    if original_name:
        # Pre-flight validation on the name alone (extension allowlist +
        # traversal check) so we never write a rejected file.
        ok, reason, ext = security.validate_upload(original_name, 0)
        if not ok:
            raise web.HTTPFound('/admin?upload_error=' + reason)

        limit = config.MAX_VIDEO_UPLOAD_SIZE if ext == "mp4" else config.MAX_UPLOAD_SIZE
        if size > limit:
            raise web.HTTPFound('/admin?upload_error=' + 'Ukuran file melebihi batas (10 MB, video 50 MB).')

        new_filename = f"{uuid.uuid4()}.{ext}"
        rel_path = new_filename  # stored relative to UPLOAD_DIR
        abs_path = os.path.join(config.UPLOAD_DIR, new_filename)
        with open(abs_path, 'wb') as f:
            for chunk in chunks:
                f.write(chunk)

        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO media_uploads (filename, filepath) VALUES (?, ?)',
                       (new_filename, rel_path))
        conn.commit()
        conn.close()
    raise web.HTTPFound('/admin')


async def handle_delete_media(request):
    data = await request.post()
    mid = data.get('id')
    if mid:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT filepath FROM media_uploads WHERE id = ?', (mid,))
        res = cursor.fetchone()
        if res:
            stored_path = res[0]
            # Use the stored path; block any traversal outside UPLOAD_DIR.
            abs_path = security.safe_upload_path(stored_path)
            if abs_path is None:
                abs_path = security.safe_upload_path(os.path.basename(stored_path))
            if abs_path and os.path.exists(abs_path):
                os.remove(abs_path)
            cursor.execute('DELETE FROM media_uploads WHERE id = ?', (mid,))
            conn.commit()
        conn.close()
    raise web.HTTPFound('/admin')


async def handle_add_pkg(request):
    data = await request.post()
    if data.get('name') and data.get('subtitle'):
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO packages (name, subtitle, image_url) VALUES (?, ?, ?)',
                       (data.get('name'), data.get('subtitle'), data.get('image_url')))
        conn.commit()
        conn.close()
    raise web.HTTPFound('/admin')


async def handle_delete_pkg(request):
    data = await request.post()
    if data.get('id'):
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM packages WHERE id = ?', (data.get('id'),))
        cursor.execute('DELETE FROM templates WHERE package_id = ?', (data.get('id'),))
        conn.commit()
        conn.close()
    raise web.HTTPFound('/admin')


async def handle_add_tmpl(request):
    data = await request.post()
    if data.get('package_id') and data.get('name') and data.get('price'):
        is_top10 = 1 if data.get('is_top10') == '1' else 0
        # Duration is NOT captured here: it comes from the tier's active_days.
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO templates (package_id, name, price, discount, image_url, html_code, is_top10, tier_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('package_id'),
            data.get('name'),
            data.get('price'),
            data.get('discount'),
            data.get('image_url'),
            data.get('html_code'),
            is_top10,
            data.get('tier_id') or None,
        ))
        conn.commit()
        conn.close()
    raise web.HTTPFound('/admin')


async def handle_delete_tmpl(request):
    data = await request.post()
    if data.get('id'):
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM templates WHERE id = ?', (data.get('id'),))
        conn.commit()
        conn.close()
    raise web.HTTPFound('/admin')


def setup(app):
    # Public auth endpoints.
    app.router.add_get('/admin/login', handle_login_form)
    app.router.add_post('/admin/login', handle_login)
    app.router.add_get('/admin/logout', handle_logout)

    # Protected admin endpoints (middleware enforces the session cookie).
    app.router.add_get('/admin', handle_admin)
    app.router.add_post('/admin/update_homepage', handle_update_homepage)
    app.router.add_post('/admin/upload_media', handle_upload_media)
    app.router.add_post('/admin/delete_media', handle_delete_media)
    app.router.add_post('/admin/add_pkg', handle_add_pkg)
    app.router.add_post('/admin/delete_pkg', handle_delete_pkg)
    app.router.add_post('/admin/add_tmpl', handle_add_tmpl)
    app.router.add_post('/admin/delete_tmpl', handle_delete_tmpl)
