"""Public (unauthenticated) routes: index, templates, demo, editor, checkout, guestbook.

All HTML is rendered through Jinja2 with autoescape ON so DB/query values can
never inject markup. URLs and behavior match the original app.py except that
the checkout form now submits via POST.
"""
import os

from aiohttp import web
from markupsafe import Markup

import config
from db import get_conn
from routes.common import BASE_HEAD, FOOTER_HTML, NAV_SCRIPT, render
from services.tiers import duration_label as _tier_duration, get_tier

# Legacy free-text values that used to live in templates.duration; the period
# now comes exclusively from the tier's active_days (services/tiers.py).
_LEGACY_DURATION_MAP = {
    'Aktif 6 Bulan': 'silver',
    'Aktif 1 Tahun': 'gold',
    'Aktif Selamanya': 'platinum',
}


def resolve_duration(tier_code, legacy_duration=''):
    """Human-readable active period ('30 hari', ...) — never from templates.duration."""
    if not tier_code and legacy_duration:
        tier_code = _LEGACY_DURATION_MAP.get(str(legacy_duration).strip())
    if not tier_code:
        tier_code = 'silver'
    try:
        return _tier_duration(tier_code)
    except KeyError:
        return _tier_duration('silver')


def _json_script(name: str, value) -> Markup:
    """Embed a value as JSON inside a <script> tag with XSS-safe slashes."""
    import json as _json
    payload = _json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")
    return Markup('<script>window.{name} = {payload};</script>').format(name=name, payload=payload)


def _js_expr(value) -> Markup:
    """Return an XSS-safe JS literal expression for use inside inline scripts."""
    import json as _json
    raw = _json.dumps(value)
    safe = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return Markup(safe)


def _hamburger(pkgs):
    """Build the dropdown 'Kategori Paket' links from (id, name[, ...]) rows."""
    html = """
        <a href="/"><i class="fa-solid fa-house" style="color:#fbbf24;"></i> Home</a>
        <div style="padding: 6px 12px; font-size: 9px; font-weight: bold; color: #71717a; text-transform: uppercase; border-top:1px solid #27272a; border-bottom:1px solid #27272a; margin-top:4px;">Kategori Paket</div>
    """
    for row in pkgs:
        pid, name = row[0], row[1]
        html += render(
            '<a href="/templates?package_id={{ pid }}"><i class="fa-solid fa-folder" style="color:#fbbf24;"></i> {{ name }}</a>',
            pid=pid, name=name,
        )
    return Markup(html)


async def handle_index(request):
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT t.id, t.name, t.price, t.discount, t.duration, t.image_url, ti.code
        FROM templates t LEFT JOIN tiers ti ON t.tier_id = ti.id
        WHERE t.is_top10 = 1 LIMIT 10
    ''')
    top10 = cursor.fetchall()

    cursor.execute('SELECT id, name, subtitle, image_url FROM packages')
    pkgs = cursor.fetchall()
    conn.close()

    if os.path.exists(config.HOMEPAGE_FILE):
        with open(config.HOMEPAGE_FILE, "r", encoding="utf-8") as f:
            custom_homepage_html = f.read()
    else:
        custom_homepage_html = "<p>File homepage.html tidak ditemukan.</p>"

    top10_html = ""
    for tid, tname, tprice, tdisc, tdur, timg, ttier in top10:
        tdur = resolve_duration(ttier, tdur)
        top10_html += render("""
        <div style="min-width: 130px; background: #18181b; border: 1px solid #27272a; border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; justify-content: space-between;">
            <div style="height: 90px; position: relative;">
                {% if tdisc %}<span style="position:absolute; top:6px; left:6px; background:#ef4444; color:#fff; font-size:8px; font-weight:bold; padding:2px 5px; border-radius:3px;">{{ tdisc }}</span>{% endif %}
                <img src="{{ timg }}" alt="{{ tname }}" style="width:100%; height:100%; object-fit:cover;">
            </div>
            <div style="padding: 8px;">
                <div style="font-size: 11px; font-weight: bold; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{{ tname }}</div>
                <div style="font-size: 10px; color: #34d399; font-weight: bold; margin-bottom: 6px;">{{ tprice }}</div>
                <a href="/template-action?id={{ tid }}" style="display:block; background:#27272a; color:#fbbf24; text-align:center; padding:4px; border-radius:4px; font-size:9px; font-weight:bold; text-decoration:none;">Pilih</a>
            </div>
        </div>
        """, tid=tid, tname=tname, tprice=tprice, tdisc=tdisc, timg=timg)

    if not top10_html:
        top10_html = "<p style='font-size:11px; color:#71717a;'>Belum ada template unggulan.</p>"

    pkg_grid_html = ""
    for pid, name, sub, img_url in pkgs:
        pkg_grid_html += render("""
        <a href="/templates?package_id={{ pid }}" style="background: #18181b; border: 1px solid #27272a; border-radius: 12px; overflow: hidden; text-decoration: none; display: flex; flex-direction: column; justify-content: space-between; transition: 0.2s;">
            <div style="height: 100px; width: 100%;">
                <img src="{{ img_url }}" alt="{{ name }}" style="width:100%; height:100%; object-fit:cover;">
            </div>
            <div style="padding: 10px;">
                <h3 style="font-size: 12px; font-weight: 700; color: #fff; margin-bottom: 2px;">{{ name }}</h3>
                <p style="font-size: 10px; color: #a1a1aa; margin-bottom: 8px; line-height: 1.2;">{{ sub }}</p>
                <div style="font-size: 10px; color: #fbbf24; font-weight: bold; display: flex; align-items: center; justify-content: space-between;">
                    <span>Lihat Koleksi</span>
                    <i class="fa-solid fa-arrow-right"></i>
                </div>
            </div>
        </a>
        """, pid=pid, name=name, sub=sub, img_url=img_url)

    html_content = render("""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        {{ base_head }}
        <style>
            .bio-card { background: linear-gradient(135deg, #18181b 0%, #121215 100%); border: 1px solid #27272a; border-radius: 14px; padding: 18px; margin-bottom: 25px; text-align: center; }
            .bio-card h4 { color: #fbbf24; font-size: 13px; font-weight: 700; margin-bottom: 6px; }
            .bio-card p { font-size: 11px; color: #a1a1aa; line-height: 1.5; margin-bottom: 12px; }
            .quote { font-style: italic; font-size: 11px; color: #d4d4d8; border-top: 1px solid #27272a; padding-top: 10px; }
            .packages-grid-2col { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
            .top10-scroll { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 10px; scrollbar-width: thin; }
            .top10-scroll::-webkit-scrollbar { height: 4px; }
            .top10-scroll::-webkit-scrollbar-thumb { background: #27272a; border-radius: 4px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="content-wrap">
                <div class="navbar">
                    <a href="/" class="brand-group">
                        <span class="brand-main">SUKA MOTO</span>
                        <span class="brand-sub">Invitation</span>
                    </a>
                    <div class="nav-actions">
                        <button class="hamburger-btn" onclick="toggleMenu()"><i class="fa-solid fa-bars"></i></button>
                        <div id="menuDropdown" class="menu-dropdown">
                            {{ hamburger_folder_html }}
                        </div>
                    </div>
                </div>

                {{ custom_homepage_html }}

                <div class="section-title">⭐ 10 Template Terbaik & Unggulan</div>
                <div class="top10-scroll">
                    {{ top10_html }}
                </div>

                <div class="section-title" style="margin-top: 25px;">Pilihan Kategori Paket</div>
                <div class="packages-grid-2col">
                    {{ pkg_grid_html }}
                </div>
            </div>

            {{ footer_html }}
        </div>
        {{ nav_script }}
    </body>
    </html>
    """,
        base_head=Markup(BASE_HEAD),
        hamburger_folder_html=_hamburger(pkgs),
        custom_homepage_html=Markup(custom_homepage_html),
        top10_html=Markup(top10_html),
        pkg_grid_html=Markup(pkg_grid_html),
        footer_html=Markup(FOOTER_HTML),
        nav_script=Markup(NAV_SCRIPT),
    )
    return web.Response(text=html_content, content_type='text/html')


async def handle_templates(request):
    pkg_id = request.query.get('package_id', '1')

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT name FROM packages WHERE id = ?', (pkg_id,))
    pkg_res = cursor.fetchone()
    pkg_name = pkg_res[0] if pkg_res else "Koleksi Paket"

    cursor.execute('''
        SELECT t.id, t.name, t.price, t.discount, t.duration, t.image_url, ti.code
        FROM templates t LEFT JOIN tiers ti ON t.tier_id = ti.id
        WHERE t.package_id = ?
    ''', (pkg_id,))
    tmpls = cursor.fetchall()

    cursor.execute('SELECT id, name FROM packages')
    pkgs = cursor.fetchall()
    conn.close()

    grid_html = ""
    for tid, tname, tprice, tdisc, tdur, timg, ttier in tmpls:
        tdur = resolve_duration(ttier, tdur)
        grid_html += render("""
        <div class="tmpl-card">
            <div class="tmpl-img">
                {% if tdisc %}<span class="badge-disc">{{ tdisc }}</span>{% endif %}
                <img src="{{ timg }}" alt="{{ tname }}">
            </div>
            <div class="tmpl-info">
                <div>
                    <h4>{{ tname }}</h4>
                    <span class="tmpl-price">{{ tprice }}</span>
                    {% if tdur %}<span class="tmpl-dur"><i class="fa-regular fa-clock"></i> {{ tdur }}</span>{% endif %}
                </div>
                <a href="/template-action?id={{ tid }}" class="preview-btn">Pilih Template</a>
            </div>
        </div>
        """, tid=tid, tname=tname, tprice=tprice, tdisc=tdisc, tdur=tdur, timg=timg)

    if not grid_html:
        grid_html = "<p style='grid-column: span 2; text-align:center; color:#71717a; font-size:12px; padding:30px;'>Belum ada template di paket ini.</p>"

    html_content = render("""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        {{ base_head }}
        <style>
            .back-link { font-size: 11px; color: #fbbf24; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; margin-bottom: 15px; font-weight: bold; }
            .page-heading { margin-bottom: 20px; }
            .page-heading h2 { font-size: 16px; color: #fff; font-weight: 700; }
            .page-heading p { font-size: 11px; color: #a1a1aa; }
            .template-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
            .tmpl-card { background: #18181b; border: 1px solid #27272a; border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; justify-content: space-between; }
            .tmpl-img { width: 100%; height: 110px; overflow: hidden; position: relative; }
            .tmpl-img img { width: 100%; height: 100%; object-fit: cover; }
            .badge-disc { position: absolute; top: 8px; left: 8px; background: #ef4444; color: #fff; font-size: 9px; font-weight: bold; padding: 2px 6px; border-radius: 4px; }
            .tmpl-info { padding: 10px; display: flex; flex-direction: column; flex-grow: 1; justify-content: space-between; }
            .tmpl-info h4 { font-size: 12px; color: #fff; font-weight: 600; margin-bottom: 2px; line-height: 1.3; }
            .tmpl-price { font-size: 11px; color: #34d399; font-weight: bold; display: block; margin-bottom: 4px; }
            .tmpl-dur { font-size: 10px; color: #a1a1aa; display: block; margin-bottom: 8px; }
            .preview-btn { background: #27272a; color: #fbbf24; text-align: center; padding: 6px; border-radius: 6px; text-decoration: none; font-size: 10px; font-weight: bold; transition: background 0.2s; display: block; }
            .preview-btn:hover { background: #fbbf24; color: #000; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="content-wrap">
                <div class="navbar">
                    <a href="/" class="brand-group">
                        <span class="brand-main">SUKA MOTO</span>
                        <span class="brand-sub">Invitation</span>
                    </a>
                    <div class="nav-actions">
                        <button class="hamburger-btn" onclick="toggleMenu()"><i class="fa-solid fa-bars"></i></button>
                        <div id="menuDropdown" class="menu-dropdown">
                            {{ hamburger_folder_html }}
                        </div>
                    </div>
                </div>

                <a href="/" class="back-link"><i class="fa-solid fa-arrow-left"></i> Kembali ke Beranda</a>

                <div class="page-heading">
                    <h2>{{ pkg_name }}</h2>
                    <p>Pilih template eksklusif sesuai tema pernikahan Anda</p>
                </div>

                <div class="template-grid">
                    {{ grid_html }}
                </div>
            </div>

            {{ footer_html }}
        </div>
        {{ nav_script }}
    </body>
    </html>
    """,
        base_head=Markup(BASE_HEAD),
        hamburger_folder_html=_hamburger(pkgs),
        pkg_name=pkg_name,
        grid_html=Markup(grid_html),
        footer_html=Markup(FOOTER_HTML),
        nav_script=Markup(NAV_SCRIPT),
    )
    return web.Response(text=html_content, content_type='text/html')


async def handle_template_action(request):
    tmpl_id = request.query.get('id')
    if not tmpl_id:
        return web.Response(text="Template tidak ditemukan", status=404)

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.name, t.price, t.discount, t.duration, t.image_url, ti.code
        FROM templates t LEFT JOIN tiers ti ON t.tier_id = ti.id
        WHERE t.id = ?
    ''', (tmpl_id,))
    tmpl = cursor.fetchone()

    cursor.execute('SELECT id, name FROM packages')
    pkgs = cursor.fetchall()
    conn.close()

    if not tmpl:
        return web.Response(text="Template tidak terdaftar", status=404)

    tname, tprice, tdisc, tdur, timg, ttier = tmpl
    tdur = resolve_duration(ttier, tdur)

    html_content = render("""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        {{ base_head }}
        <style>
            .action-card { background: #18181b; border: 1px solid #27272a; border-radius: 14px; padding: 20px; text-align: center; margin-top: 15px; }
            .action-img { width: 100%; height: 160px; border-radius: 8px; overflow: hidden; margin-bottom: 15px; }
            .action-img img { width: 100%; height: 100%; object-fit: cover; }
            .btn-demo { display: block; background: #27272a; color: #fff; padding: 12px; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 12px; margin-bottom: 10px; border: 1px solid #3f3f46; transition: 0.2s; }
            .btn-demo:hover { background: #3f3f46; }
            .btn-build { display: block; background: #fbbf24; color: #000; padding: 12px; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 12px; transition: 0.2s; }
            .btn-build:hover { background: #f59e0b; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="content-wrap">
                <div class="navbar">
                    <a href="/" class="brand-group">
                        <span class="brand-main">SUKA MOTO</span>
                        <span class="brand-sub">Invitation</span>
                    </a>
                    <div class="nav-actions">
                        <button class="hamburger-btn" onclick="toggleMenu()"><i class="fa-solid fa-bars"></i></button>
                        <div id="menuDropdown" class="menu-dropdown">
                            {{ hamburger_folder_html }}
                        </div>
                    </div>
                </div>

                <a href="/" style="font-size:11px; color:#fbbf24; text-decoration:none; font-weight:bold;"><i class="fa-solid fa-arrow-left"></i> Kembali</a>

                <div class="action-card">
                    <div class="action-img">
                        <img src="{{ timg }}" alt="{{ tname }}">
                    </div>
                    <h2 style="font-size: 16px; color: #fff; margin-bottom: 4px;">{{ tname }}</h2>
                    {% if tdisc %}<div style="color:#ef4444; font-size:11px; font-weight:bold; margin-bottom:5px;">{{ tdisc }}</div>{% endif %}
                    <div style="font-size: 14px; color: #34d399; font-weight: bold; margin-bottom: 4px;">{{ tprice }}</div>
                    <div style="font-size: 11px; color: #a1a1aa; margin-bottom: 20px;"><i class="fa-regular fa-clock"></i> {{ tdur }}</div>

                    <a href="/demo?id={{ tmpl_id }}" class="btn-demo" target="_blank"><i class="fa-solid fa-eye"></i> Lihat Demo Template</a>
                    <a href="/editor?id={{ tmpl_id }}" class="btn-build"><i class="fa-solid fa-wand-magic-sparkles"></i> Buat Undangan Ini</a>
                </div>
            </div>
            {{ footer_html }}
        </div>
        {{ nav_script }}
    </body>
    </html>
    """,
        base_head=Markup(BASE_HEAD),
        hamburger_folder_html=_hamburger(pkgs),
        tname=tname, tprice=tprice, tdisc=tdisc, tdur=tdur, timg=timg, tmpl_id=tmpl_id,
        footer_html=Markup(FOOTER_HTML),
        nav_script=Markup(NAV_SCRIPT),
    )
    return web.Response(text=html_content, content_type='text/html')


async def handle_demo(request):
    tmpl_id = request.query.get('id')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT html_code FROM templates WHERE id = ?', (tmpl_id,))
    res = cursor.fetchone()
    conn.close()

    if not res or not res[0]:
        return web.Response(text="Demo tidak ditemukan", status=404)

    protected_html = res[0].replace("<body>", """<body>
    <script>
        document.addEventListener('contextmenu', event => event.preventDefault());
        document.onkeydown = function(e) {
            if(e.keyCode == 123 || (e.ctrlKey && e.shiftKey && (e.keyCode == 73 || e.keyCode == 74)) || (e.ctrlKey && e.keyCode == 85)) {
                return false;
            }
        }
    </script>
    <div style="position:fixed; bottom:10px; right:10px; background:rgba(0,0,0,0.7); color:#fbbf24; font-size:10px; padding:4px 8px; border-radius:4px; z-index:9999;">SUKA MOTO DEMO MODE</div>
    """)
    return web.Response(text=protected_html, content_type='text/html')


async def handle_editor(request):
    tmpl_id = request.query.get('id')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT name, price, html_code FROM templates WHERE id = ?', (tmpl_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return web.Response(text="Template tidak ditemukan", status=404)

    tname, tprice, html_code = res
    editor_banner = render("""
    <div id="sukamoto-builder-bar" style="position:fixed; top:0; left:0; width:100%; background:#18181b; border-bottom:1px solid #27272a; padding:10px 15px; display:flex; justify-content:space-between; align-items:center; z-index:999999; box-shadow:0 4px 6px rgba(0,0,0,0.3);">
        <div style="font-size:12px; color:#fff; font-weight:bold;">Editor: {{ tname }} <span style="color:#34d399; margin-left:8px;">{{ tprice }}</span></div>
        <div>
            <span style="font-size:10px; color:#fbbf24; margin-right:10px;">Klik teks untuk mengedit langsung!</span>
            <a href="/checkout?id={{ tmpl_id }}" style="background:#fbbf24; color:#000; padding:6px 12px; border-radius:6px; font-size:11px; font-weight:bold; text-decoration:none;">Lanjut ke Pembayaran &rarr;</a>
        </div>
    </div>
    <div style="height:50px;"></div>
    """, tname=tname, tprice=tprice, tmpl_id=tmpl_id)
    modified_html = html_code.replace("<body>", f"<body>{editor_banner}")
    return web.Response(text=modified_html, content_type='text/html')


async def handle_checkout(request):
    tmpl_id = request.query.get('id')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.name, t.price, t.discount, t.duration, ti.code
        FROM templates t LEFT JOIN tiers ti ON t.tier_id = ti.id
        WHERE t.id = ?
    ''', (tmpl_id,))
    tmpl = cursor.fetchone()
    conn.close()

    if not tmpl:
        return web.Response(text="Data checkout tidak valid", status=404)

    tname, tprice, tdisc, tdur, ttier = tmpl
    tdur = resolve_duration(ttier, tdur)
    html_content = render("""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        {{ base_head }}
        <style>
            .checkout-box { background: #18181b; border: 1px solid #27272a; border-radius: 12px; padding: 20px; margin-top: 15px; }
            .summary-row { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 10px; color: #a1a1aa; }
            .summary-row.total { font-size: 14px; color: #34d399; font-weight: bold; border-top: 1px solid #27272a; padding-top: 10px; margin-top: 10px; }
            input, select { padding: 10px; margin: 6px 0; border-radius: 8px; border: 1px solid #3f3f46; background: #121215; color: #fff; width: 100%; font-size: 12px; }
            .pay-btn { display: block; width: 100%; background: #34d399; color: #000; font-weight: bold; padding: 12px; border-radius: 8px; border: none; margin-top: 15px; cursor: pointer; text-align: center; text-decoration: none; font-size: 13px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="content-wrap">
                <div class="navbar">
                    <a href="/" class="brand-group">
                        <span class="brand-main">SUKA MOTO</span>
                        <span class="brand-sub">Invitation</span>
                    </a>
                </div>

                <div class="section-title">Konfirmasi Pembayaran & Pesanan</div>

                <div class="checkout-box">
                    <h3 style="font-size: 14px; color: #fff; margin-bottom: 12px;">Ringkasan Paket</h3>
                    <div class="summary-row"><span>Template</span><strong style="color:#fff;">{{ tname }}</strong></div>
                    <div class="summary-row"><span>Durasi Aktif</span><strong style="color:#fff;">{{ tdur }}</strong></div>
                    <div class="summary-row"><span>Promo/Diskon</span><strong style="color:#ef4444;">{{ tdisc or 'Tidak ada' }}</strong></div>
                    <div class="summary-row total"><span>Total Pembayaran</span><span>{{ tprice }}</span></div>
                </div>

                <div class="checkout-box" style="margin-top: 15px;">
                    <h3 style="font-size: 14px; color: #fff; margin-bottom: 10px;">Data Pemesan</h3>
                    <form action="/guestbook" method="POST">
                        <input type="hidden" name="id" value="{{ tmpl_id }}">
                        <label style="font-size:11px; color:#a1a1aa;">Nama Lengkap / Mempelai:</label>
                        <input type="text" name="buyer_name" placeholder="Contoh: Rian & Siska" required>
                        <label style="font-size:11px; color:#a1a1aa; margin-top:8px; display:block;">Nomor WhatsApp:</label>
                        <input type="text" name="whatsapp" placeholder="Contoh: 08123456789" required>
                        <label style="font-size:11px; color:#a1a1aa; margin-top:8px; display:block;">Metode Pembayaran:</label>
                        <select name="bank">
                            <option value="BCA">Transfer BCA - 1234567890 a.n SUKA MOTO</option>
                            <option value="DANA">QRIS / DANA - 085156918852</option>
                        </select>
                        <button type="submit" class="pay-btn">Simulasi Bayar & Lanjut ke Buku Tamu &rarr;</button>
                    </form>
                </div>
            </div>
            {{ footer_html }}
        </div>
    </body>
    </html>
    """,
        base_head=Markup(BASE_HEAD),
        tname=tname, tprice=tprice, tdisc=tdisc, tdur=tdur, tmpl_id=tmpl_id,
        footer_html=Markup(FOOTER_HTML),
    )
    return web.Response(text=html_content, content_type='text/html')


async def handle_guestbook(request):
    # Accept both POST (new checkout flow) and GET (legacy links).
    if request.method == "POST":
        data = await request.post()
        tmpl_id = data.get('id')
        buyer_name = data.get('buyer_name') or 'Mempelai'
    else:
        tmpl_id = request.query.get('id')
        buyer_name = request.query.get('buyer_name', 'Mempelai')

    tmpl_id_js = _js_expr(tmpl_id if tmpl_id is not None else "")
    buyer_name_js = _js_expr(buyer_name)

    page_js = """
        <script>
            var GB_BASE_ID = {{ tmpl_id_js }};
            var GB_BUYER = {{ buyer_name_js }};

            function esc(s) {
                return String(s).replace(/[&<>"']/g, function(c) {
                    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
                });
            }

            function generateLinks() {
                const text = document.getElementById('guestListInput').value.trim();
                const container = document.getElementById('resultContainer');
                if(!text) {
                    alert('Silakan masukkan minimal satu nama tamu!');
                    return;
                }

                const names = text.split('\\n');
                let html = '';
                const baseUrl = window.location.origin + '/demo?id=' + encodeURIComponent(GB_BASE_ID) + '&to=';

                names.forEach((name, index) => {
                    const cleanName = name.trim();
                    if(cleanName) {
                        const specificUrl = baseUrl + encodeURIComponent(cleanName);
                        const waText = encodeURIComponent(`Halo *${cleanName}*, tanpa mengurangi rasa hormat, kami mengundang Bapak/Ibu/Saudara/i untuk menghadiri acara pernikahan ${GB_BUYER}. Berikut link undangan digital kami:\\n\\n${specificUrl}`);
                        const waUrl = `https://wa.me/?text=${waText}`;

                        html += `
                        <div class="guest-item">
                            <div>
                                <strong style="color:#fff; display:block; margin-bottom:2px;">${esc(cleanName)}</strong>
                                <span style="font-size:9px; color:#71717a; word-break:break-all;">${esc(specificUrl)}</span>
                            </div>
                            <div class="link-btns">
                                <button data-url="${esc(specificUrl)}" data-name="${esc(cleanName)}">Salin</button>
                                <a href="${esc(waUrl)}" target="_blank" class="wa"><i class="fa-brands fa-whatsapp"></i> WA</a>
                            </div>
                        </div>
                        `;
                    }
                });
                container.innerHTML = html;
                container.querySelectorAll('button[data-url]').forEach(btn => {
                    btn.addEventListener('click', () => {
                        navigator.clipboard.writeText(btn.getAttribute('data-url'));
                        alert('Link untuk ' + btn.getAttribute('data-name') + ' disalin!');
                    });
                });
            }
        </script>
    """
    page_js = render(page_js, tmpl_id_js=tmpl_id_js, buyer_name_js=buyer_name_js)

    html_content = render("""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        {{ base_head }}
        <style>
            .gb-box { background: #18181b; border: 1px solid #27272a; border-radius: 12px; padding: 18px; margin-top: 15px; }
            textarea { width: 100%; height: 90px; background: #121215; border: 1px solid #3f3f46; color: #fff; border-radius: 8px; padding: 10px; font-size: 12px; resize: vertical; }
            .btn-action { background: #fbbf24; color: #000; font-weight: bold; border: none; padding: 10px; border-radius: 8px; width: 100%; cursor: pointer; margin-top: 10px; font-size: 12px; }
            .guest-item { background: #121215; border: 1px solid #27272a; padding: 10px; border-radius: 8px; margin-top: 8px; display: flex; justify-content: space-between; align-items: center; font-size: 11px; }
            .link-btns { display: flex; gap: 5px; }
            .link-btns a, .link-btns button { background: #27272a; color: #fbbf24; border: none; padding: 5px 8px; border-radius: 4px; font-size: 10px; cursor: pointer; text-decoration: none; font-weight: bold; }
            .link-btns a.wa { background: #22c55e; color: #fff; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="content-wrap">
                <div class="navbar">
                    <a href="/" class="brand-group">
                        <span class="brand-main">SUKA MOTO</span>
                        <span class="brand-sub">Invitation</span>
                    </a>
                </div>

                <div class="section-title">Custom Buku Tamu & Generator Link</div>

                <div class="gb-box">
                    <h3 style="font-size: 13px; color: #fff; margin-bottom: 6px;">Input Daftar Tamu Undangan</h3>
                    <p style="font-size: 11px; color: #a1a1aa; margin-bottom: 12px;">Masukkan nama-nama tamu (satu nama per baris) agar sistem otomatis membuatkan link personal.</p>
                    <textarea id="guestListInput" placeholder="Bpk. Budi Santoso&#10;Ibu Siti Aminah&#10;Rian & Partner"></textarea>
                    <button type="button" class="btn-action" onclick="generateLinks()">Generate Link Tamu</button>
                </div>

                <div class="gb-box" style="margin-top: 15px;">
                    <h3 style="font-size: 13px; color: #fff; margin-bottom: 10px;">Daftar Link Undangan Spesifik Tamu</h3>
                    <div id="resultContainer" style="max-height: 250px; overflow-y: auto;">
                        <p style="font-size: 11px; color: #71717a; text-align: center; padding: 15px;">Belum ada tamu di-generate. Masukkan nama di atas lalu klik tombol generate.</p>
                    </div>
                </div>
            </div>
            {{ footer_html }}
        </div>

        {{ page_js }}
    </body>
    </html>
    """,
        base_head=Markup(BASE_HEAD),
        footer_html=Markup(FOOTER_HTML),
        page_js=Markup(page_js),
    )
    return web.Response(text=html_content, content_type='text/html')


def setup(app):
    app.router.add_get('/', handle_index)
    app.router.add_get('/templates', handle_templates)
    app.router.add_get('/template-action', handle_template_action)
    app.router.add_get('/demo', handle_demo)
    app.router.add_get('/editor', handle_editor)
    app.router.add_get('/checkout', handle_checkout)
    app.router.add_get('/guestbook', handle_guestbook)
    app.router.add_post('/guestbook', handle_guestbook)
