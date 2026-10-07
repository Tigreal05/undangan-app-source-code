"""Public (unauthenticated) routes: index, templates, demo, editor, checkout, guestbook.

All HTML is rendered through Jinja2 with autoescape ON so DB/query values can
never inject markup. URLs and behavior match the original app.py except that
the checkout form now submits via POST.
"""
import json
import os
import re
from datetime import datetime, timedelta

from aiohttp import web
from markupsafe import Markup, escape as _html_esc

import config
from db import get_conn
from routes.common import BASE_HEAD, FOOTER_HTML, NAV_SCRIPT, render
from services import editor_schema as editor_svc
from services import orders as orders_svc
from services import payments as payments_svc
from services import render as render_svc
from services import template_resolver as resolver_svc
from services.tiers import duration_label as _tier_duration, get_tier

#: Human-readable order status labels (kept in sync with the admin inbox).
STATUS_LABELS = {
    orders_svc.DRAFT: "Dibuat",
    orders_svc.NEW: "Baru",
    orders_svc.PENDING_PAYMENT: "Menunggu Pembayaran",
    orders_svc.PAYMENT_REPORTED: "Bukti Diterima",
    orders_svc.PAID: "Dikonfirmasi",
    orders_svc.IN_PROTECTION: "Perlindungan",
    orders_svc.PUBLISHED: "Undangan Aktif",
    orders_svc.EXPIRED: "Kedaluwarsa",
    orders_svc.CANCELLED: "Dibatalkan",
}

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


#: Protection script used ONLY by the legacy demo path (html_code templates).
#: New templates get their watermark/protection from render.decorate_mode().
_LEGACY_PROTECT_HTML = """<body>
    <script>
        document.addEventListener('contextmenu', event => event.preventDefault());
        document.onkeydown = function(e) {
            if(e.keyCode == 123 || (e.ctrlKey && e.shiftKey && (e.keyCode == 73 || e.keyCode == 74)) || (e.ctrlKey && e.keyCode == 85)) {
                return false;
            }
        }
    </script>
    <div style="position:fixed; bottom:10px; right:10px; background:rgba(0,0,0,0.7); color:#fbbf24; font-size:10px; padding:4px 8px; border-radius:4px; z-index:9999;">SUKA MOTO DEMO MODE</div>
    """


def _legacy_demo_html(html_code):
    """Legacy /demo behaviour: inject protection + watermark after <body>."""
    return html_code.replace("<body>", _LEGACY_PROTECT_HTML)


async def handle_demo(request):
    """GET /demo?id=<template_id>[&to=Nama Tamu].

    Phase 1 migration: the DB row is resolved through services.template_resolver.
    * source="new"    → rendered via services.render.render_invitation with
                        preview.json data and mode="demo" (watermark and
                        protection are added once by decorate_mode — never
                        duplicated here).
    * source="legacy" → unchanged legacy behaviour: templates.html_code with
                        the inline protection/wrapper below.
    """
    tmpl_id = request.query.get('id')
    guest_name = request.query.get('to')
    try:
        resolved = resolver_svc.resolve_template_by_id(tmpl_id)
    except resolver_svc.TemplateResolutionError:
        return web.Response(text="Demo tidak ditemukan", status=404)

    if resolved["source"] == resolver_svc.SOURCE_NEW:
        # Demo data comes exclusively from preview.json (never hardcoded here).
        preview_data = resolver_svc.load_preview_data(resolved)
        try:
            html = render_svc.render_invitation(
                template_dir=resolved["template_dir"],
                data=preview_data or None,
                theme=preview_data.get("theme") if preview_data else None,
                tier_code=resolved["tier_code"] or "silver",
                mode="demo",
                guest_name=guest_name,
            )
        except Exception:
            # Broken new source must never take the demo page down: fall back
            # to the legacy html_code when one exists.
            if not resolved["html_code"]:
                return web.Response(text="Demo tidak ditemukan", status=404)
            return web.Response(
                text=_legacy_demo_html(resolved["html_code"]),
                content_type='text/html')
        return web.Response(text=html, content_type='text/html')

    # Legacy template (not yet converted): keep the old demo behaviour as-is.
    if not resolved["html_code"]:
        return web.Response(text="Demo tidak ditemukan", status=404)
    return web.Response(text=_legacy_demo_html(resolved["html_code"]),
                        content_type='text/html')


async def handle_editor(request):
    """GET /editor?id=<template_id>[&draft=<invitation_id>]

    Phase 2 migration (schema-driven editor):

    * source="new"    → data editor generated from schema.json. Initial data
                        comes from an existing draft (content_json) when one
                        is available for this editing context, otherwise from
                        preview.json (read-only sample — never written back).
                        Live preview goes through POST /editor/preview and
                        saving through POST /editor/save.
    * source="legacy" → unchanged legacy contenteditable behaviour over
                        templates.html_code (gradual migration; the legacy
                        path is removed in a later phase).
    """
    tmpl_id = request.query.get('id')
    try:
        resolved = resolver_svc.resolve_template_by_id(tmpl_id)
    except resolver_svc.TemplateResolutionError:
        return web.Response(text="Template tidak ditemukan", status=404)

    if resolved["source"] == resolver_svc.SOURCE_NEW:
        return await _editor_new(request, resolved)

    # ---- Legacy editor (html_code + contenteditable) — kept as-is ----------
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


# ---------------------------------------------------------------------------
# Schema-driven editor (Phase 2) — new templates only
# ---------------------------------------------------------------------------

def _draft_identity(request):
    """Opaque token that ties repeated saves to the same browser session.

    LIMITATION (documented honestly): the app has no client authentication
    yet, so drafts are identified by (template, this random cookie, the
    explicit ?draft= id echoed by the editor page). Anyone who obtains the
    cookie/token could reopen their own draft; there is no multi-user
    isolation until a real client account system lands in a later phase.
    """
    token = request.cookies.get("suka_draft_token")
    if not token or not _DRAFT_TOKEN_RE.match(token):
        token = orders_svc.unique_code(length=16)
    return token


_DRAFT_TOKEN_RE = re.compile(r"^[a-z0-9]{8,64}$")


def _find_existing_draft(cursor, template_id, token):
    """Draft invitation belonging to this (token, template) editing context."""
    cursor.execute(
        "SELECT id FROM invitations"
        " WHERE state = 'draft' AND custom = 0"
        "   AND json_extract(content_json, '$._meta.token') = ?"
        "   AND json_extract(content_json, '$._meta.template_id') = ?"
        " ORDER BY id DESC LIMIT 1",
        (token, str(template_id)))
    row = cursor.fetchone()
    return row[0] if row else None


_EDITOR_PAGE_HTML = """
<!DOCTYPE html>
<html lang="id">
<head>
    {{ base_head }}
    <title>Editor — {{ tname }}</title>
    <style>
        .editor-wrap { display: flex; gap: 16px; align-items: flex-start; }
        .editor-form { flex: 1 1 320px; min-width: 0; }
        .editor-preview { flex: 1 1 360px; position: sticky; top: 10px; }
        .editor-preview iframe { width: 100%; height: 80vh; border: 1px solid #27272a; border-radius: 12px; background: #fff; }
        .ed-group { background: #18181b; border: 1px solid #27272a; border-radius: 12px; padding: 14px; margin-bottom: 12px; }
        .ed-group h3 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #fbbf24; margin-bottom: 10px; }
        .ed-field { margin-bottom: 10px; }
        .ed-field label { display: block; font-size: 11px; color: #a1a1aa; margin-bottom: 4px; }
        .ed-field input, .ed-field textarea, .ed-field select {
            width: 100%; padding: 9px; border-radius: 8px; border: 1px solid #3f3f46;
            background: #121215; color: #fff; font-size: 12px; }
        .ed-field textarea { min-height: 70px; resize: vertical; }
        .ed-error { color: #f87171; font-size: 10px; margin-top: 3px; display: none; }
        .ed-item { border-top: 1px dashed #3f3f46; padding-top: 8px; margin-top: 8px; }
        .ed-actions { display: flex; gap: 8px; margin-top: 14px; }
        .ed-btn { flex: 1; padding: 11px; border-radius: 8px; border: none; font-weight: bold; font-size: 12px; cursor: pointer; }
        .ed-save { background: #34d399; color: #000; }
        .ed-preview-btn { background: #18181b; color: #fbbf24; border: 1px solid #fbbf24; }
        #ed-status { font-size: 11px; color: #a1a1aa; margin-top: 8px; min-height: 16px; }
        .ed-note { font-size: 10px; color: #71717a; margin-top: 6px; }
    </style>
</head>
<body>
    {{ config_js }}
    <div class="container">
        <div class="content-wrap">
            <div class="navbar">
                <a href="/" class="brand-group">
                    <span class="brand-main">SUKA MOTO</span>
                    <span class="brand-sub">Invitation</span>
                </a>
            </div>
            <div class="section-title">Edit Undangan — {{ tname }}</div>
            <div class="editor-wrap">
                <div class="editor-form">
                    <form id="ed-form" onsubmit="return false;"></form>
                    <div class="ed-actions">
                        <button type="button" class="ed-btn ed-preview-btn" id="ed-preview">Preview</button>
                        <button type="button" class="ed-btn ed-save" id="ed-save">Simpan Draft</button>
                    </div>
                    <div id="ed-status"></div>
                    <div class="ed-note">Draft tersimpan di server; preview bersifat sementara dan tidak mengubah data tersimpan.</div>
                </div>
                <div class="editor-preview">
                    <iframe id="ed-frame" title="Preview undangan" sandbox="allow-scripts allow-same-origin"></iframe>
                </div>
            </div>
        </div>
        {{ footer_html }}
    </div>
    <script>
    (function () {
        var FORM = window.__EDITOR_FORM__;
        var DRAFT_BLOB = window.__EDITOR_DATA__ || {data: {}, theme: {}};
        var DATA = DRAFT_BLOB.data || {};
        DATA.theme = DRAFT_BLOB.theme || {};
        var form = document.getElementById('ed-form');
        var status = document.getElementById('ed-status');
        var frame = document.getElementById('ed-frame');

        function el(tag, attrs, children) {
            var n = document.createElement(tag);
            Object.keys(attrs || {}).forEach(function (k) {
                if (k === 'text') n.textContent = attrs[k];
                else if (k === 'html') n.innerHTML = attrs[k];
                else n.setAttribute(k, attrs[k]);
            });
            (children || []).forEach(function (c) { n.appendChild(c); });
            return n;
        }

        function fieldNode(f, value, namePrefix) {
            var wrap = el('div', {'class': 'ed-field'});
            var name = (namePrefix || '') + (f.name || f.key);
            wrap.appendChild(el('label', {for: 'fld-' + name.replace(/[^a-zA-Z0-9]/g, '_'),
                                          text: f.label + (f.required ? ' *' : '')}));
            var input;
            var v = value == null ? '' : String(value);
            if (f.type === 'textarea') {
                input = el('textarea', {name: name, rows: 3});
                input.value = v;
            } else if (f.type === 'select') {
                input = el('select', {name: name});
                input.appendChild(el('option', {value: '', text: '— pilih —'}));
                (f.options || []).forEach(function (opt) {
                    var o = el('option', {value: opt, text: opt});
                    if (opt === v) o.selected = true;
                    input.appendChild(o);
                });
            } else if (f.type === 'color') {
                input = el('input', {type: 'text', name: name, placeholder: '#rrggbb'});
                input.value = v;
                if ((f.presets || []).length) {
                    var dl = el('datalist', {id: 'dl-' + name.replace(/[^a-zA-Z0-9]/g, '_')});
                    f.presets.forEach(function (p) { dl.appendChild(el('option', {value: p})); });
                    input.setAttribute('list', dl.id);
                    wrap.appendChild(dl);
                }
            } else if (f.type === 'image') {
                input = el('input', {type: 'url', name: name, placeholder: 'https://…'});
                input.value = v;
            } else {
                var t = f.type === 'date' ? 'date' : (f.type === 'time' ? 'time' : 'text');
                input = el('input', {type: t, name: name});
                if (t !== 'date' && t !== 'time') input.value = v;
                else input.value = v;
            }
            if (f.max_length) input.maxLength = f.max_length;
            input.id = 'fld-' + name.replace(/[^a-zA-Z0-9]/g, '_');
            wrap.appendChild(input);
            wrap.appendChild(el('div', {'class': 'ed-error', 'data-for': name}));
            return wrap;
        }

        function collectRepeater(rep) {
            var items = [];
            form.querySelectorAll('[data-rep="' + rep.key + '"]').forEach(function (box) {
                var item = {};
                box.querySelectorAll('[name]').forEach(function (inp) {
                    item[inp.getAttribute('data-key')] = inp.value;
                });
                items.push(item);
            });
            return items;
        }

        function collectData() {
            var data = {};
            FORM.groups.forEach(function (g) {
                g.fields.forEach(function (f) {
                    var inp = form.querySelector('[name="' + (f.name || f.key) + '"]');
                    if (inp) data[f.key] = inp.value;
                });
            });
            FORM.repeatables.forEach(function (rep) {
                if (!rep.available) return;
                data[rep.key] = collectRepeater(rep);
            });
            var theme = {};
            FORM.theme.forEach(function (f) {
                if (!f.available) return;
                var inp = form.querySelector('[name="' + f.name + '"]');
                if (inp && inp.value.trim()) theme[f.key] = inp.value.trim();
            });
            data.theme = theme;
            return data;
        }

        function showErrors(errors) {
            form.querySelectorAll('.ed-error').forEach(function (e) { e.style.display = 'none'; e.textContent = ''; });
            Object.keys(errors || {}).forEach(function (k) {
                var node = form.querySelector('.ed-error[data-for="' + k + '"]');
                if (node) { node.textContent = errors[k]; node.style.display = 'block'; }
            });
        }

        // Build groups
        FORM.groups.forEach(function (g) {
            var box = el('div', {'class': 'ed-group'});
            box.appendChild(el('h3', {text: g.label}));
            g.fields.forEach(function (f) { box.appendChild(fieldNode(f, DATA[f.key], '')); });
            form.appendChild(box);
        });

        // Build repeatables
        FORM.repeatables.forEach(function (rep) {
            if (!rep.available) return;
            var box = el('div', {'class': 'ed-group'});
            box.appendChild(el('h3', {text: rep.label}));
            (DATA[rep.key] || []).forEach(function (item) {
                box.appendChild(repeaterItem(rep, item));
            });
            var add = el('button', {type: 'button', 'class': 'ed-btn ed-preview-btn', text: '+ Tambah'});
            add.addEventListener('click', function () {
                box.insertBefore(repeaterItem(rep, {}), add);
            });
            box.appendChild(add);
            form.appendChild(box);
        });

        function repeaterItem(rep, item) {
            var wrap = el('div', {'class': 'ed-item', 'data-rep': rep.key});
            rep.fields.forEach(function (f) {
                var node = fieldNode(f, item[f.key], rep.key + '.');
                node.querySelector('[name]').setAttribute('data-key', f.key);
                node.querySelector('[name]').setAttribute('name', rep.key + '.' + f.key);
                node.querySelector('.ed-error').setAttribute('data-for', rep.key + '[0].' + f.key);
                wrap.appendChild(node);
            });
            var rm = el('button', {type: 'button', text: 'Hapus', style: 'font-size:10px;background:none;border:none;color:#f87171;cursor:pointer;'});
            rm.addEventListener('click', function () { wrap.remove(); });
            wrap.appendChild(rm);
            return wrap;
        }

        // Theme group
        var themeAvail = FORM.theme.filter(function (f) { return f.available; });
        if (themeAvail.length) {
            var tbox = el('div', {'class': 'ed-group'});
            tbox.appendChild(el('h3', {text: 'Tema'}));
            themeAvail.forEach(function (f) {
                var val = (DATA.theme || {})[f.key] || '';
                tbox.appendChild(fieldNode({key: f.key, name: f.name, label: f.label, type: f.type,
                                             required: false, max_length: 60, options: f.options,
                                             presets: f.presets}, val, ''));
            });
            form.appendChild(tbox);
        }

        var lastPreviewHtml = null;
        function postEditor(path, extra) {
            var body = {template_id: {{ tmpl_id_js }}, data: collectData()};
            if (window.__EDITOR_DRAFT_ID__) body.draft_id = window.__EDITOR_DRAFT_ID__;
            Object.assign(body, extra || {});
            return fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body), credentials: 'same-origin'})
                .then(function (r) { return r.json().then(function (j) { return {status: r.status, json: j}; }); });
        }

        function refreshPreview() {
            return postEditor('/editor/preview').then(function (res) {
                if (res.json.ok) {
                    lastPreviewHtml = res.json.html;
                    frame.srcdoc = res.json.html;
                    showErrors({});
                } else {
                    showErrors(res.json.errors || {});
                    status.textContent = res.json.error || 'Preview gagal.';
                }
                return res;
            });
        }

        document.getElementById('ed-preview').addEventListener('click', function () {
            status.textContent = 'Memperbarui preview…';
            refreshPreview().then(function () { status.textContent = ''; });
        });

        document.getElementById('ed-save').addEventListener('click', function () {
            status.textContent = 'Menyimpan draft…';
            postEditor('/editor/save').then(function (res) {
                if (res.json.ok) {
                    window.__EDITOR_DRAFT_ID__ = res.json.invitation_id;
                    status.textContent = 'Draft tersimpan (ID ' + res.json.invitation_id + ').';
                    showErrors({});
                    if (lastPreviewHtml) frame.srcdoc = lastPreviewHtml;
                    else refreshPreview();
                } else {
                    status.textContent = res.json.error || 'Gagal menyimpan.';
                    showErrors(res.json.errors || {});
                }
            }).catch(function () { status.textContent = 'Kesalahan jaringan.'; });
        });

        // Debounced live preview while typing.
        var debounce = null;
        form.addEventListener('input', function () {
            clearTimeout(debounce);
            debounce = setTimeout(refreshPreview, 700);
        });

        refreshPreview();
    })();
    </script>
    {{ nav_script }}
</body>
</html>
"""


async def _editor_new(request, resolved):
    """Render the schema-driven editor page for a converted template."""
    template_dir = resolved["template_dir"]
    tier_code = resolved["tier_code"] or "silver"
    schema = editor_svc.load_schema(template_dir)
    if not schema:
        # Template dir exists but has no usable schema: refuse rather than
        # fall back to rendering user HTML; demo/legacy paths still work.
        return web.Response(text="Schema template tidak tersedia", status=500)

    token = _draft_identity(request)

    # Existing draft for this editing context? (?draft= echo wins, then token.)
    draft_id = None
    requested_draft = (request.query.get('draft') or '').strip()
    data, theme = {}, {}
    conn = get_conn()
    try:
        cursor = conn.cursor()
        if requested_draft.isdigit():
            cursor.execute(
                "SELECT id, content_json FROM invitations"
                " WHERE id = ? AND state = 'draft'", (int(requested_draft),))
            row = cursor.fetchone()
            if row:
                draft_id = row[0]
                data, theme = editor_svc.draft_data(row[1])
        if draft_id is None:
            draft_id = _find_existing_draft(cursor, resolved["id"], token)
            if draft_id is not None:
                cursor.execute(
                    "SELECT content_json FROM invitations WHERE id = ?", (draft_id,))
                data, theme = editor_svc.draft_data(cursor.fetchone()[0])
    finally:
        conn.close()

    if not data:
        # First open without a draft: preview.json is the initial data ONLY.
        data = dict(resolver_svc.load_preview_data(resolved))
        theme = dict(data.pop("theme", {}) or {})
        data.pop("slug", None)

    form_meta = editor_svc.build_form_meta(schema, tier_code)
    # Data + draft-id must be defined BEFORE the builder script runs: emit
    # them right after <body>, together with the schema metadata.
    data_js = (_json_script("__EDITOR_DATA__", {"data": data, "theme": theme})
               + _json_script("__EDITOR_DRAFT_ID__", draft_id)
               + _json_script("__EDITOR_FORM__", form_meta))
    html_content = render(
        _EDITOR_PAGE_HTML,
        base_head=Markup(BASE_HEAD),
        footer_html=Markup(FOOTER_HTML),
        nav_script=Markup(NAV_SCRIPT),
        tname=resolved["name"],
        tmpl_id_js=_js_expr(int(resolved["id"])),
        config_js=data_js,
    )
    resp = web.Response(text=html_content, content_type='text/html')
    # Persist the draft identity cookie (HttpOnly, SameSite=Lax).
    resp.set_cookie("suka_draft_token", token, httponly=True, samesite="Lax",
                    max_age=30 * 24 * 3600)
    return resp


async def _read_editor_payload(request):
    """Parse + sanity-check the JSON body shared by preview/save endpoints."""
    if request.content_length and request.content_length > editor_svc.MAX_PAYLOAD_BYTES:
        return None, {"error": "Payload terlalu besar."}
    try:
        payload = await request.json()
    except Exception:
        return None, {"error": "Body harus JSON yang valid."}
    if not isinstance(payload, dict):
        return None, {"error": "Body harus objek JSON."}
    template_id = payload.get("template_id")
    if not isinstance(template_id, int) or template_id <= 0:
        return None, {"error": "template_id tidak valid."}
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, {"error": "Field data harus berupa objek."}
    return payload, None


def _resolve_new_template(template_id):
    """Resolver lookup restricted to converted templates (never arbitrary
    filesystem paths — everything flows through template_resolver)."""
    resolved = resolver_svc.resolve_template_by_id(template_id)
    if resolved["source"] != resolver_svc.SOURCE_NEW:
        raise resolver_svc.TemplateSourceUnavailable(
            f"template {template_id} belum memiliki source baru")
    return resolved


async def handle_editor_preview(request):
    """POST /editor/preview — transient render of current editor data.

    NEVER touches the database: no invitation, no order, no draft writes.
    """
    payload, err = await _read_editor_payload(request)
    if err:
        return web.json_response({"ok": False, **err}, status=400)
    try:
        resolved = _resolve_new_template(payload["template_id"])
    except resolver_svc.TemplateResolutionError:
        return web.json_response({"ok": False, "error": "Template tidak ditemukan."},
                                 status=404)

    schema = editor_svc.load_schema(resolved["template_dir"])
    tier_code = resolved["tier_code"] or "silver"
    try:
        data, theme = editor_svc.parse_and_validate(payload["data"], schema, tier_code)
    except editor_svc.EditorValidationError as exc:
        return web.json_response({"ok": False, "errors": exc.errors}, status=422)

    try:
        html = render_svc.render_invitation(
            template_dir=resolved["template_dir"],
            data=data,
            theme=theme,
            tier_code=tier_code,
            mode="preview",
        )
    except Exception:
        return web.json_response({"ok": False, "error": "Gagal merender preview."},
                                 status=500)
    return web.json_response({"ok": True, "html": html})


async def handle_editor_save(request):
    """POST /editor/save — create/update the draft invitation atomically.

    Repeated saves from the same editing context update the SAME invitation
    (identity: draft_id echoed by the client, else the HttpOnly token cookie
    scoped to the template). A minimal parent order in state ``draft`` is
    created inside the same transaction because invitations.order_id is NOT
    NULL; it is never marked paid and nothing is published here.
    """
    payload, err = await _read_editor_payload(request)
    if err:
        return web.json_response({"ok": False, **err}, status=400)
    try:
        resolved = _resolve_new_template(payload["template_id"])
    except resolver_svc.TemplateResolutionError:
        return web.json_response({"ok": False, "error": "Template tidak ditemukan."},
                                 status=404)

    schema = editor_svc.load_schema(resolved["template_dir"])
    tier_code = resolved["tier_code"] or "silver"
    try:
        data, theme = editor_svc.parse_and_validate(payload["data"], schema, tier_code)
    except editor_svc.EditorValidationError as exc:
        return web.json_response({"ok": False, "errors": exc.errors}, status=422)

    token = _draft_identity(request)
    content = json.loads(editor_svc.draft_content(data, theme))
    content["_meta"] = {"token": token, "template_id": str(resolved["id"]),
                        "code": resolved["code"]}
    content_blob = json.dumps(content, ensure_ascii=False)

    conn = get_conn()
    try:
        cursor = conn.cursor()
        draft_id = payload.get("draft_id")
        existing = None
        if isinstance(draft_id, int) and draft_id > 0:
            # Honour the client echo only when it really is a draft owned by
            # this token/template — never hijack somebody else's row.
            cursor.execute(
                "SELECT id FROM invitations WHERE id = ? AND state = 'draft'"
                " AND json_extract(content_json, '$._meta.token') = ?",
                (draft_id, token))
            row = cursor.fetchone()
            existing = row[0] if row else None
        if existing is None:
            existing = _find_existing_draft(cursor, resolved["id"], token)

        now = orders_svc._now_str()
        couple_names = (data.get("title") or "").strip() or "Mempelai"
        event_date = data.get("event", {}).get("date_iso", "") if isinstance(data.get("event"), dict) else data.get("date_iso", "")
        if not event_date:
            event_date = data.get("date_label", "") or data.get("date", "")
        event_time = data.get("time_range", "") or data.get("time", "")
        venue_name = ""
        for ev in data.get("events", []) or []:
            if isinstance(ev, dict) and ev.get("venue_name"):
                venue_name = ev["venue_name"]
                break

        if existing is not None:
            cursor.execute(
                "UPDATE invitations SET content_json = ?, title = ?,"
                " couple_names = ?, event_date = ?, event_time = ?,"
                " venue_name = ?, colors_json = ?, updated_at = ?,"
                " state = 'draft' WHERE id = ? AND state = 'draft'",
                (content_blob, f"The Wedding of {couple_names}", couple_names,
                 event_date, event_time, venue_name,
                 json.dumps(theme, ensure_ascii=False), now, existing))
            conn.commit()
            invitation_id = existing
        else:
            # --- atomic creation: draft order + draft invitation ---------
            order_id = None
            try:
                code = orders_svc.new_order_code(conn)
                cursor.execute(
                    "INSERT INTO orders (code, tier_id, template_id, status)"
                    " VALUES (?, ?, ?, ?)",
                    (code, resolved["tier_id"], resolved["id"], orders_svc.DRAFT))
                order_id = cursor.lastrowid

                # Draft slug: unique, clearly non-public (publishing will
                # regenerate the final slug in a later phase).
                slug = orders_svc.unique_code(length=12, conn=conn)
                cursor.execute(
                    "INSERT INTO invitations (order_id, slug, title, couple_names,"
                    " event_date, event_time, venue_name, colors_json, content_json,"
                    " state, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                    (order_id, slug, f"The Wedding of {couple_names}", couple_names,
                     event_date, event_time, venue_name,
                     json.dumps(theme, ensure_ascii=False), content_blob, now, now))
                invitation_id = cursor.lastrowid
                orders_svc.log_order_event(
                    conn, order_id, actor="client", action="draft_created",
                    from_status="", to_status=orders_svc.DRAFT,
                    note=f"template={resolved['code']}")
                conn.commit()
            except Exception:
                conn.rollback()
                # Rollback completed safely — return a structured JSON error
                # (no orphan order, no partial invitation) instead of letting
                # the exception escape into aiohttp's plaintext 500 handler.
                return web.json_response(
                    {"ok": False, "error": "Gagal menyimpan draft."}, status=500)
    finally:
        conn.close()

    resp = web.json_response({"ok": True, "invitation_id": invitation_id})
    resp.set_cookie("suka_draft_token", token, httponly=True, samesite="Lax",
                    max_age=30 * 24 * 3600)
    return resp


async def handle_checkout(request):
    tmpl_id = request.query.get('id')
    draft_id = request.query.get('draft')
    if draft_id is not None and not str(draft_id).isdigit():
        draft_id = None

    # --- Draft ownership check (Phase 3.1) ---------------------------------
    # A draft can only be checked out by the editing context that owns it:
    # the HttpOnly suka_draft_token cookie must match content_json._meta.token
    # AND the draft's template must match the requested template id. The
    # client cannot forge this with a hidden input — validation is purely
    # server-side. Without a matching draft there is NO fallback to any
    # other invitation (no IDOR).
    draft = None
    if draft_id is not None:
        token = _draft_identity(request)
        conn = get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT i.id, i.order_id, i.title, i.couple_names,"
                " i.event_date, i.event_time, i.venue_name,"
                " json_extract(i.content_json, '$._meta.template_id'),"
                " o.status"
                " FROM invitations i JOIN orders o ON o.id = i.order_id"
                " WHERE i.id = ? AND i.state = 'draft'"
                "   AND json_extract(i.content_json, '$._meta.token') = ?",
                (int(draft_id), token))
            row = cursor.fetchone()
            if row is None or str(row[7]) != str(tmpl_id):
                return web.json_response(
                    {"ok": False, "error": "Draft tidak ditemukan atau bukan milik Anda."},
                    status=403)
            if row[8] != orders_svc.DRAFT:
                return web.json_response(
                    {"ok": False, "error": "Pesanan untuk draft ini sudah diproses."},
                    status=409)
            draft = row
        finally:
            conn.close()

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

    # Server-side price truth (Phase 3.1): the amount shown here is computed
    # from DB tier/template prices via services/orders.py — never from the
    # client. The same values are re-read server-side at confirmation time.
    uniq_preview = orders_svc.payment_unique_code()
    total_preview = orders_svc.compute_total(tprice, tdisc, uniq_preview)

    if draft is not None:
        summary_rows = (
            "<div class=\"summary-row\"><span>Judul Undangan</span><strong style=\"color:#fff;\">{}</strong></div>"
            "<div class=\"summary-row\"><span>Pasangan</span><strong style=\"color:#fff;\">{}</strong></div>"
            "<div class=\"summary-row\"><span>Tanggal</span><strong style=\"color:#fff;\">{}</strong></div>"
            "<div class=\"summary-row\"><span>Waktu</span><strong style=\"color:#fff;\">{}</strong></div>"
            "<div class=\"summary-row\"><span>Tempat</span><strong style=\"color:#fff;\">{}</strong></div>"
        ).format(
            _html_esc(draft[2] or ''), _html_esc(draft[3] or ''),
            _html_esc(draft[4] or ''), _html_esc(draft[5] or ''),
            _html_esc(draft[6] or '-'))
        hidden_draft = f'<input type="hidden" name="draft_id" value="{int(draft[0])}">'
        submit_label = "Konfirmasi Pesanan &rarr;"
    else:
        summary_rows = ""
        hidden_draft = ""
        submit_label = "Simulasi Bayar & Lanjut ke Buku Tamu &rarr;"

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
                    <div class="summary-row total"><span>Total Pembayaran</span><span>{{ total_label }}</span></div>
                </div>

                <div class="checkout-box" style="margin-top: 15px;">
                    <h3 style="font-size: 14px; color: #fff; margin-bottom: 10px;">Data Pemesan</h3>
                    {{ summary_rows }}
                    <form action="/checkout/confirm" method="POST">
                        <input type="hidden" name="id" value="{{ tmpl_id }}">
                        {{ hidden_draft }}
                        <label style="font-size:11px; color:#a1a1aa;">Nama Lengkap / Mempelai:</label>
                        <input type="text" name="buyer_name" placeholder="Contoh: Rian & Siska" required>
                        <label style="font-size:11px; color:#a1a1aa; margin-top:8px; display:block;">Nomor WhatsApp:</label>
                        <input type="text" name="whatsapp" placeholder="Contoh: 08123456789" required>
                        <label style="font-size:11px; color:#a1a1aa; margin-top:8px; display:block;">Metode Pembayaran:</label>
                        <select name="bank">
                            <option value="BCA">Transfer BCA - 1234567890 a.n SUKA MOTO</option>
                            <option value="DANA">QRIS / DANA - 085156918852</option>
                        </select>
                        <button type="submit" class="pay-btn">{{ submit_label | safe }}</button>
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
        total_label=orders_svc.format_rupiah(total_preview),
        summary_rows=Markup(summary_rows),
        hidden_draft=Markup(hidden_draft),
        submit_label=submit_label,
        footer_html=Markup(FOOTER_HTML),
    )
    return web.Response(text=html_content, content_type='text/html')


async def handle_checkout_confirm(request):
    """POST /checkout/confirm — draft order -> pending_payment (Phase 3.1).

    Server-side truth only: template/tier/price are re-read from the DB, the
    invitation is located through the authenticated draft identity (cookie +
    _meta.token), never through client-supplied amounts or statuses. The whole
    lifecycle move runs in ONE transaction with rollback on any failure.
    Idempotent: an already-confirmed draft returns its existing order instead
    of creating a duplicate.
    """
    try:
        data = await request.post()
    except Exception:
        return web.json_response({"ok": False, "error": "Form tidak valid."}, status=400)

    tmpl_id = data.get('id')
    buyer_name = (data.get('buyer_name') or '').strip()[:120]
    whatsapp = (data.get('whatsapp') or '').strip()[:32]
    payment_method = (data.get('bank') or '').strip()[:20]
    if not tmpl_id or not str(tmpl_id).isdigit():
        return web.json_response({"ok": False, "error": "Template tidak valid."}, status=400)
    tmpl_id = int(tmpl_id)
    if not buyer_name:
        return web.json_response({"ok": False, "error": "Nama pemesan wajib diisi."}, status=422)
    if payment_method not in ("BCA", "DANA"):
        return web.json_response({"ok": False, "error": "Metode pembayaran tidak dikenal."}, status=422)

    token = _draft_identity(request)
    conn = get_conn()
    try:
        cursor = conn.cursor()
        orders_svc.apply_lazy_expiry(conn)

        # Locate the draft invitation owned by THIS editing context.
        cursor.execute(
            "SELECT i.id, i.order_id FROM invitations i"
            " WHERE i.state = 'draft' AND i.custom = 0"
            "   AND json_extract(i.content_json, '$._meta.token') = ?"
            "   AND json_extract(i.content_json, '$._meta.template_id') = ?"
            " ORDER BY i.id DESC LIMIT 1",
            (token, str(tmpl_id)))
        row = cursor.fetchone()
        if row is None:
            return web.json_response(
                {"ok": False, "error": "Tidak ada draft untuk dikonfirmasi. Simpan draft terlebih dahulu."},
                status=403)
        invitation_id, order_id = row

        cursor.execute(
            "SELECT code, status, tier_id, template_id FROM orders WHERE id = ?",
            (order_id,))
        order = cursor.fetchone()
        if order is None:
            return web.json_response({"ok": False, "error": "Order tidak ditemukan."}, status=404)
        order_code, status, tier_id, order_template_id = order

        # --- Idempotency: already confirmed? Return the same order. --------
        if status == orders_svc.PENDING_PAYMENT:
            return web.Response(
                text=f'<meta http-equiv="refresh" content="0; url=/checkout/status?order={order_code}">',
                content_type='text/html')
        if status != orders_svc.DRAFT:
            return web.json_response(
                {"ok": False, "error": f"Pesanan tidak dapat dikonfirmasi dari status '{status}'."},
                status=409)

        # --- Price truth: ONLY from the DB (template + tier), never form. --
        cursor.execute(
            "SELECT t.price, t.discount FROM templates t"
            " LEFT JOIN tiers ti ON t.tier_id = ti.id WHERE t.id = ?",
            (order_template_id if order_template_id is not None else tmpl_id,))
        tmpl_row = cursor.fetchone()
        if tmpl_row is None:
            return web.json_response({"ok": False, "error": "Template tidak ditemukan."}, status=404)
        price_text, discount_text = tmpl_row

        puc = orders_svc.payment_unique_code(conn)
        if not puc:
            return web.json_response(
                {"ok": False, "error": "Kode pembayaran sedang penuh. Silakan coba lagi."},
                status=503)
        total = orders_svc.compute_total(price_text, discount_text, puc)

        now = orders_svc._now_str()
        deadline = (datetime.now() + timedelta(
            hours=orders_svc.PAYMENT_DEADLINE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            cursor.execute(
                "UPDATE orders SET status = ?, buyer_name = ?, whatsapp = ?,"
                " payment_method = ?, amount = ?, payment_unique_code = ?,"
                " payment_deadline = ?, updated_at = ? WHERE id = ? AND status = ?",
                (orders_svc.PENDING_PAYMENT, buyer_name, whatsapp, payment_method,
                 orders_svc.format_rupiah(total), puc, deadline, now,
                 order_id, orders_svc.DRAFT))
            if cursor.rowcount != 1:
                # Someone else moved this order concurrently — abort cleanly.
                raise orders_svc.IllegalTransition("order status changed concurrently")
            # Keep the linked invitation consistent with the draft state.
            cursor.execute(
                "UPDATE invitations SET updated_at = ? WHERE id = ?", (now, invitation_id))
            orders_svc.log_order_event(
                conn, order_id, actor="client", action="transition:pending_payment",
                from_status=orders_svc.DRAFT, to_status=orders_svc.PENDING_PAYMENT,
                note=f"checkout invitation={invitation_id}")
            conn.commit()
        except Exception:
            conn.rollback()
            return web.json_response(
                {"ok": False, "error": "Gagal membuat pesanan. Silakan coba lagi."},
                status=500)
    finally:
        conn.close()

    return web.HTTPSeeOther(f"/checkout/status?order={order_code}")


async def handle_checkout_status(request):
    """GET /checkout/status?order=<code> — client-facing order status page.

    Ownership: the page only reveals details when the order's linked draft
    invitation belongs to THIS editing context (cookie token == content_json
    ._meta.token). Unknown/non-owned orders get a generic 403/404 — no admin
    data is exposed.
    """
    code = request.query.get('order', '')
    if not code or len(str(code)) > 32:
        return web.Response(text="Pesanan tidak ditemukan", status=404)

    token = _draft_identity(request)
    conn = get_conn()
    try:
        cursor = conn.cursor()
        orders_svc.apply_lazy_expiry(conn)
        cursor.execute(
            "SELECT o.id, o.code, o.status, o.buyer_name, o.amount,"
            " o.payment_method, o.payment_unique_code, o.payment_deadline,"
            " t.name, o.template_id"
            " FROM orders o"
            " LEFT JOIN templates t ON t.id = o.template_id"
            " WHERE o.code = ?", (str(code),))
        row = cursor.fetchone()
        if row is None:
            return web.Response(text="Pesanan tidak ditemukan", status=404)
        (oid, oc, status, buyer, amount, method, puc, deadline, tname_, tmpl_id) = row

        cursor.execute(
            "SELECT 1 FROM invitations WHERE order_id = ?"
            " AND json_extract(content_json, '$._meta.token') = ?",
            (oid, token))
        mine = cursor.fetchone() is not None
    finally:
        conn.close()

    if not mine:
        return web.json_response(
            {"ok": False, "error": "Pesanan bukan milik Anda."}, status=403)

    status_label = STATUS_LABELS.get(status, status)
    html_content = render("""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        {{ base_head }}
        <style>
            .st-box { background: #18181b; border: 1px solid #27272a; border-radius: 12px; padding: 20px; margin-top: 15px; }
            .st-row { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 10px; color: #a1a1aa; }
            .st-badge { display:inline-block; padding: 4px 10px; border-radius: 999px; font-size: 11px; font-weight: bold; background: #fbbf24; color: #000; }
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
                <div class="section-title">Status Pesanan</div>
                <div class="st-box">
                    <p style="font-size:13px; color:#34d399; font-weight:bold; margin-bottom:12px;">Pesanan berhasil dibuat.</p>
                    <div class="st-row"><span>Kode Pesanan</span><strong style="color:#fff;">{{ oc }}</strong></div>
                    <div class="st-row"><span>Status</span><span class="st-badge">{{ status_label }}</span></div>
                    <div class="st-row"><span>Pemesan</span><strong style="color:#fff;">{{ buyer }}</strong></div>
                    <div class="st-row"><span>Template</span><strong style="color:#fff;">{{ tname_ }}</strong></div>
                    <div class="st-row"><span>Total Pembayaran</span><strong style="color:#34d399;">{{ amount }}</strong></div>
                    <div class="st-row"><span>Metode</span><strong style="color:#fff;">{{ method }}</strong></div>
                    {% if puc %}<div class="st-row"><span>Kode Unik Pembayaran</span><strong style="color:#fbbf24;">{{ puc }}</strong></div>{% endif %}
                    {% if deadline %}<div class="st-row"><span>Batas Bayar</span><strong style="color:#fff;">{{ deadline }}</strong></div>{% endif %}
                </div>
                <div class="st-box">
                    <p style="font-size:12px; color:#a1a1aa;">
                        Pembayaran Anda <strong style="color:#fbbf24;">belum dikonfirmasi</strong>.
                        Buka halaman pembayaran untuk melihat instruksi QRIS / transfer bank
                        dan mengunggah bukti pembayaran.
                    </p>
                    <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:10px;">
                        <a href="/payment?order={{ oc }}" style="display:inline-block; background:#34d399; color:#000; padding:8px 14px; border-radius:8px; font-size:12px; font-weight:bold; text-decoration:none;">Bayar Sekarang &rarr;</a>
                        <a href="/editor?id={{ tmpl_id }}" style="display:inline-block; background:#fbbf24; color:#000; padding:8px 14px; border-radius:8px; font-size:12px; font-weight:bold; text-decoration:none;">Kembali ke Editor</a>
                    </div>
                </div>
            </div>
            {{ footer_html }}
        </div>
    </body>
    </html>
    """,
        base_head=Markup(BASE_HEAD),
        oc=oc, status_label=status_label, buyer=buyer or "-",
        tname_=tname_ or "-", amount=amount or "-", method=method or "-",
        puc=puc, deadline=deadline, tmpl_id=tmpl_id if tmpl_id is not None else "",
        footer_html=Markup(FOOTER_HTML),
    )
    return web.Response(text=html_content, content_type='text/html')


# ---------------------------------------------------------------------------
# Phase 3.2 — customer payment submission (QRIS / bank transfer + proof)
# ---------------------------------------------------------------------------

PAYMENT_METHOD_LABELS = {"qris": "QRIS", "bank_transfer": "Transfer Bank"}


def _owned_order_row(cursor, token, order_code):
    """Fetch an order the CURRENT editing context owns (same cookie-token
    ownership rule as /checkout/status). Returns row tuple or None."""
    cursor.execute(
        "SELECT o.id, o.code, o.status, o.buyer_name, o.amount,"
        " o.payment_method, o.payment_unique_code, o.payment_deadline,"
        " t.name, o.template_id"
        " FROM orders o"
        " LEFT JOIN templates t ON t.id = o.template_id"
        " WHERE o.code = ?", (str(order_code),))
    row = cursor.fetchone()
    if row is None:
        return None
    owned = cursor.execute(
        "SELECT 1 FROM invitations WHERE order_id = ?"
        " AND json_extract(content_json, '$._meta.token') = ? LIMIT 1",
        (row[0], token)).fetchone()
    return row if owned else None


async def handle_payment_page(request):
    """GET /payment?order=<code> — instructions + proof upload for the owner.

    Authorization reuses the Phase 3.1 draft-identity mechanism (HttpOnly
    suka_draft_token cookie matched against content_json._meta.token).
    Amounts always come from the authoritative order row — never client input.
    """
    code = request.query.get('order', '')
    if not code or len(str(code)) > 32:
        return web.Response(text="Pesanan tidak ditemukan", status=404)

    token = _draft_identity(request)
    conn = get_conn()
    try:
        cursor = conn.cursor()
        orders_svc.apply_lazy_expiry(conn)
        row = _owned_order_row(cursor, token, code)
        if row is None:
            exists = cursor.execute(
                "SELECT 1 FROM orders WHERE code = ?", (str(code),)).fetchone()
            return web.json_response(
                {"ok": False, "error": "Pesanan tidak ditemukan atau bukan milik Anda."},
                status=404 if not exists else 403)
        oid, oc, status, buyer, amount, method_db, puc, deadline, tname_, tmpl_id = row

        if status == orders_svc.DRAFT:
            return web.json_response(
                {"ok": False, "error": "Pesanan belum dikonfirmasi. Selesaikan checkout terlebih dahulu."},
                status=409)
        if status not in (orders_svc.PENDING_PAYMENT, orders_svc.PAYMENT_REPORTED):
            return web.json_response(
                {"ok": False, "error": f"Pembayaran tidak tersedia untuk status '{status}'."},
                status=409)

        settings = orders_svc.get_settings(conn)
        payment = payments_svc.get_active_payment(cursor, oid)
        provenance = payments_svc.parse_provenance(payment) if payment else {}
        server_total = payments_svc.order_total(conn, oid)
    finally:
        conn.close()

    # Server-rendered methods from configuration only; no client control.
    qris_img = ""
    if settings.get("qris_path"):
        qris_img = security_safe_upload_url(settings["qris_path"])
    method_cards = []
    if qris_img:
        method_cards.append(("qris", "QRIS",
                             f'<img src="{qris_img}" alt="Kode QRIS" '
                             'style="max-width:220px; border-radius:12px; background:#fff; padding:8px;">'
                             f'<p style="font-size:11px; color:#a1a1aa;">Merchant: '
                             f'{_html_esc(settings.get("bank_holder") or "SUKA MOTO")}</p>'))
    method_cards.append(("bank_transfer", "Transfer Bank",
                         f'<div class="pm-row"><span>Bank</span><strong>{_html_esc(settings.get("bank_name") or "-")}</strong></div>'
                         f'<div class="pm-row"><span>No. Rekening</span><strong>{_html_esc(settings.get("bank_number") or "-")}</strong></div>'
                         f'<div class="pm-row"><span>a.n</span><strong>{_html_esc(settings.get("bank_holder") or "-")}</strong></div>'))
    cards_html = "".join(
        f'<div class="pm-card"><h4 style="color:#fbbf24; font-size:13px; margin-bottom:8px;">{label}</h4>{body}</div>'
        for _, label, body in method_cards)

    status_note = ""
    if status == orders_svc.PAYMENT_REPORTED:
        status_note = ('<p style="font-size:12px; color:#38bdf8; font-weight:bold;">'
                       '✓ Bukti pembayaran berhasil dikirim.</p>'
                       '<p style="font-size:12px; color:#a1a1aa;">'
                       'Status: Menunggu verifikasi pembayaran oleh admin.</p>')
    elif payment and payment["status"] == payments_svc.SUBMITTED:
        status_note = ('<p style="font-size:12px; color:#a1a1aa;">Bukti sudah diunggah. '
                       'Menunggu verifikasi admin.</p>')

    uploaded_info = ""
    if provenance:
        uploaded_info = (f'<p style="font-size:11px; color:#71717a; margin-top:8px;">'
                         f'Terakhir: {_html_esc(provenance.get("original_name", ""))} '
                         f'({_html_esc(provenance.get("mime", ""))}, '
                         f'{int(provenance.get("size", 0)) // 1024} KB)</p>')

    html_content = render("""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        {{ base_head }}
        <style>
            .pm-box { background: #18181b; border: 1px solid #27272a; border-radius: 12px; padding: 20px; margin-top: 15px; }
            .pm-row { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 8px; color: #a1a1aa; }
            .pm-card { border: 1px solid #27272a; border-radius: 10px; padding: 14px; margin-top: 12px; }
            .pm-btn { display:block; width:100%; background:#34d399; color:#000; font-weight:bold; padding:12px; border-radius:8px; border:none; margin-top:15px; cursor:pointer; text-align:center; font-size:13px; }
            input[type=file] { background:#121215; color:#fff; padding:8px; border-radius:8px; border:1px solid #3f3f46; width:100%; font-size:12px; }
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
                <div class="section-title">Pembayaran Pesanan</div>
                {% if err %}<div class="pm-box" style="border-color:#ef4444;"><p style="font-size:12px; color:#ef4444;">{{ err }}</p></div>{% endif %}
                <div class="pm-box">
                    <div class="pm-row"><span>Kode Pesanan</span><strong style="color:#fff;">{{ oc }}</strong></div>
                    <div class="pm-row"><span>Pemesan</span><strong style="color:#fff;">{{ buyer }}</strong></div>
                    <div class="pm-row"><span>Template</span><strong style="color:#fff;">{{ tname_ }}</strong></div>
                    <div class="pm-row"><span>Status Pesanan</span><strong style="color:#fbbf24;">{{ status_label }}</strong></div>
                    <div class="pm-row"><span>Total Pembayaran</span><strong style="color:#34d399;">{{ total_label }}</strong></div>
                    {% if puc %}<div class="pm-row"><span>Kode Unik</span><strong style="color:#fbbf24;">{{ puc }}</strong></div>{% endif %}
                    {% if deadline %}<div class="pm-row"><span>Batas Bayar</span><strong style="color:#fff;">{{ deadline }}</strong></div>{% endif %}
                </div>
                <div class="pm-box">
                    <h3 style="font-size:14px; color:#fff; margin-bottom:4px;">Metode Pembayaran</h3>
                    <p style="font-size:11px; color:#a1a1aa;">Silakan lakukan pembayaran sesuai metode yang dipilih, kemudian upload bukti pembayaran.</p>
                    {{ cards_html }}
                </div>
                <div class="pm-box">
                    <h3 style="font-size:14px; color:#fff; margin-bottom:10px;">Upload Bukti Pembayaran</h3>
                    {{ status_note }}
                    <form action="/payment/proof" method="POST" enctype="multipart/form-data">
                        <input type="hidden" name="order" value="{{ oc }}">
                        <input type="file" name="file" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" required>
                        <p style="font-size:10px; color:#71717a; margin-top:6px;">Format JPG/PNG/WEBP, maksimal 5 MB.</p>
                        <button type="submit" class="pm-btn">Kirim Bukti Pembayaran</button>
                    </form>
                    {{ uploaded_info }}
                    <p style="font-size:10px; color:#71717a; margin-top:10px;">Catatan: mengirim bukti belum berarti pembayaran terverifikasi. Admin akan memverifikasi manual.</p>
                </div>
            </div>
            {{ footer_html }}
        </div>
    </body>
    </html>
    """,
        base_head=Markup(BASE_HEAD),
        err=request.query.get('err', '')[:200],
        oc=oc, buyer=buyer or "-", tname_=tname_ or "-",
        status_label=STATUS_LABELS.get(status, status),
        total_label=orders_svc.format_rupiah(server_total),
        puc=puc, deadline=deadline,
        cards_html=Markup(cards_html),
        status_note=Markup(status_note),
        uploaded_info=Markup(uploaded_info),
        footer_html=Markup(FOOTER_HTML),
    )
    return web.Response(text=html_content, content_type='text/html')


def security_safe_upload_url(rel_path: str) -> str:
    """Admin media files live under UPLOAD_DIR which IS served at
    /static_uploads/. Only expose a URL when the file exists inside the root
    (traversal-blocked via security.safe_upload_path)."""
    import security
    abs_path = security.safe_upload_path(rel_path or "")
    if not abs_path or not os.path.isfile(abs_path):
        return ""
    return "/static_uploads/" + os.path.basename(abs_path)


class _ProofAlreadySubmitted(web.HTTPException):
    """Internal control-flow signal: a duplicate browser upload answers with
    the payment page + inline error banner (no overwrite, no new row)."""

    def __init__(self, order_code: str):
        super().__init__(
            location=f"/payment?order={order_code}&err="
                     "Bukti+sudah+dikirim.+Tidak+perlu+kirim+ulang.")


async def handle_payment_proof_upload(request):
    """POST /payment/proof — secure payment-proof upload (Phase 3.2).

    * ownership enforced server-side (cookie token vs invitation _meta.token),
    * amount NEVER taken from the form,
    * file validated by magic bytes + extension agreement + 5 MB cap,
    * stored under UPLOAD_DIR/payment-proof/<order-id>/payment_<random>.<ext>
      (never a public static directory, never the original filename),
    * payment.status -> 'submitted' (+submitted_at); order moves through the
      EXISTING state machine pending_payment -> payment_reported in ONE
      transaction with rollback. The order is NEVER marked paid here and the
      invitation is NEVER published.
    """
    order_code = ""
    field_name = None
    data = b""
    try:
        reader = await request.multipart()
        field = await reader.next()
        while field is not None:
            if field.name == "order":
                raw = await field.read(decode=True)
                if isinstance(raw, (bytes, bytearray)):
                    raw = bytes(raw).decode("utf-8", "replace")
                order_code = str(raw or "").strip()[:32]
            elif field.name == "file" and field.filename:
                field_name = field.filename
                # Bounded streaming read: stop after MAX_PROOF_SIZE + 1 byte
                # so an oversized upload can never exhaust memory. The exact
                # 5 MB cap is enforced (again) inside store_proof().
                chunks = []
                size = 0
                while True:
                    chunk = await field.read_chunk(8192)   # raw bytes, no decoding
                    if not chunk:
                        break
                    size += len(chunk)
                    chunks.append(chunk)
                    if size > payments_svc.MAX_PROOF_SIZE:
                        break           # oversize — rejected by the check below
                data = b"".join(chunks)
            field = await reader.next()
    except Exception:
        return web.json_response({"ok": False, "error": "Upload tidak valid."},
                                 status=400)

    if not order_code or not str(order_code).strip():
        return web.json_response({"ok": False, "error": "Pesanan tidak valid."},
                                 status=400)
    if len(data) > payments_svc.MAX_PROOF_SIZE:
        return web.json_response(
            {"ok": False,
             "error": f"Ukuran file maksimal {payments_svc.MAX_PROOF_SIZE // (1024 * 1024)} MB."},
            status=422)
    if not data:
        return web.json_response({"ok": False, "error": "File bukti pembayaran wajib diisi."},
                                 status=422)

    token = _draft_identity(request)
    conn = get_conn()
    try:
        cursor = conn.cursor()
        orders_svc.apply_lazy_expiry(conn)
        row = _owned_order_row(cursor, token, order_code.strip())
        if row is None:
            exists = cursor.execute(
                "SELECT 1 FROM orders WHERE code = ?", (str(order_code).strip(),)).fetchone()
            return web.json_response(
                {"ok": False, "error": "Pesanan tidak ditemukan atau bukan milik Anda."},
                status=404 if not exists else 403)
        oid, oc, status = row[0], row[1], row[2]

        if status != orders_svc.PENDING_PAYMENT:
            return web.json_response(
                {"ok": False,
                 "error": ("Bukti sudah dikirim dan sedang menunggu verifikasi."
                           if status == orders_svc.PAYMENT_REPORTED
                           else f"Pembayaran tidak tersedia untuk status '{status}'.")},
                status=409)

        cursor.execute("SELECT payment_method FROM orders WHERE id = ?", (oid,))
        method_row = cursor.fetchone()
        method_db = (method_row[0] if method_row and method_row[0] else "")

        # Deterministic duplicate guard: an already-submitted active payment
        # must never be overwritten by a second upload.
        payment = payments_svc.get_active_payment(cursor, oid)
        if payment is not None and payment["status"] == payments_svc.SUBMITTED:
            return web.json_response(
                {"ok": False,
                 "error": "Bukti sudah dikirim. Tidak perlu kirim ulang."},
                status=409)

        # Validate the file BEFORE any DB write / storage.
        try:
            rel_path, ext, mime, real_size = payments_svc.store_proof(
                oid, data, field_name or "")
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=422)

        import security
        stored_abs = security.safe_upload_path(rel_path) if rel_path else None
        try:
            if payment is None:
                # No active attempt yet — create one with the order's chosen
                # method (amount always re-derived from the authoritative
                # order total; client-supplied amounts are ignored).
                payment, _created = payments_svc.ensure_payment(
                    conn, oid,
                    payments_svc.normalize_method(method_db) or "bank_transfer")
            rc = payments_svc.mark_submitted(
                conn, payment["id"], rel_path=rel_path,
                original_name=field_name or "", mime=mime, size=real_size)
            if rc != 1:
                # Lost a race against a concurrent submission — deterministic
                # duplicate outcome; keep the first proof untouched.
                conn.rollback()
                if stored_abs and os.path.isfile(stored_abs):
                    os.remove(stored_abs)
                raise _ProofAlreadySubmitted(oc)
            orders_svc.transition(conn, oid, orders_svc.PAYMENT_REPORTED,
                                  actor="client",
                                  actor_note="bukti pembayaran diunggah",
                                  commit=False)
            conn.commit()
        except _ProofAlreadySubmitted:
            raise
        except orders_svc.IllegalTransition:
            conn.rollback()
            if stored_abs and os.path.isfile(stored_abs):
                os.remove(stored_abs)
            return web.json_response(
                {"ok": False, "error": "Status pesanan berubah. Silakan muat ulang halaman."},
                status=409)
        except Exception:
            conn.rollback()
            if stored_abs and os.path.isfile(stored_abs):
                os.remove(stored_abs)
            return web.json_response(
                {"ok": False, "error": "Gagal menyimpan bukti pembayaran."}, status=500)
    finally:
        conn.close()

    # Browser form POST -> redirect back to the payment page which shows
    # "✓ Bukti pembayaran berhasil dikirim." + "Menunggu verifikasi".
    # Fetch/XHR (AJAX) callers get a structured JSON response instead.
    wants_json = (
        request.headers.get("Accept") == "application/json"
        or str(request.headers.get("X-Requested-With", "")).lower() == "fetch"
        or "multipart/form-data" not in (request.content_type or "")
    )
    if wants_json:
        return web.json_response({
            "ok": True,
            "order": oc,
            "payment_status": payments_svc.SUBMITTED,
            "message": "Bukti pembayaran berhasil dikirim. "
                       "Status: Menunggu verifikasi pembayaran.",
        })
    return web.HTTPSeeOther(f"/payment?order={oc}")


async def handle_payment_method_select(request):
    """POST /payment/method — choose QRIS / bank transfer (idempotent).

    Creates OR reuses the single active payment attempt for the order;
    repeated selections never pile up duplicate rows. Amount comes only
    from the authoritative order total.
    """
    try:
        post = await request.post()
    except Exception:
        return web.json_response({"ok": False, "error": "Form tidak valid."}, status=400)
    order_code = str(post.get("order") or "")[:32]
    raw_method = str(post.get("method") or "")
    method = payments_svc.normalize_method(raw_method)
    if not order_code:
        return web.json_response({"ok": False, "error": "Pesanan tidak valid."}, status=400)
    if method not in ("qris", "bank_transfer"):
        return web.json_response({"ok": False, "error": "Metode pembayaran tidak dikenal."},
                                 status=422)

    token = _draft_identity(request)
    conn = get_conn()
    try:
        cursor = conn.cursor()
        orders_svc.apply_lazy_expiry(conn)
        row = _owned_order_row(cursor, token, order_code)
        if row is None:
            exists = cursor.execute(
                "SELECT 1 FROM orders WHERE code = ?", (order_code,)).fetchone()
            return web.json_response(
                {"ok": False, "error": "Pesanan tidak ditemukan atau bukan milik Anda."},
                status=404 if not exists else 403)
        oid, oc, status = row[0], row[1], row[2]
        if status not in (orders_svc.PENDING_PAYMENT, orders_svc.PAYMENT_REPORTED):
            return web.json_response(
                {"ok": False, "error": f"Pembayaran tidak tersedia untuk status '{status}'."},
                status=409)
        try:
            payment, created = payments_svc.ensure_payment(conn, oid, method)
        except ValueError:
            return web.json_response({"ok": False, "error": "Metode pembayaran tidak dikenal."},
                                     status=422)
    finally:
        conn.close()

    resp = web.json_response({
        "ok": True, "payment_id": payment["id"], "created": created,
        "method": payment["method"], "amount": payment["amount"],
        "status": payment["status"],
    })
    resp.set_cookie("suka_draft_token", token, httponly=True, samesite="Lax",
                    max_age=30 * 24 * 3600)
    return resp


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
    app.router.add_post('/editor/preview', handle_editor_preview)
    app.router.add_post('/editor/save', handle_editor_save)
    app.router.add_get('/checkout', handle_checkout)
    app.router.add_post('/checkout/confirm', handle_checkout_confirm)
    app.router.add_get('/checkout/status', handle_checkout_status)
    app.router.add_get('/payment', handle_payment_page)
    app.router.add_post('/payment/method', handle_payment_method_select)
    app.router.add_post('/payment/proof', handle_payment_proof_upload)
    app.router.add_get('/guestbook', handle_guestbook)
    app.router.add_post('/guestbook', handle_guestbook)
