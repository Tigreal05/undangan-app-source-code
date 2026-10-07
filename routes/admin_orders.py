"""Admin order workflow (Phase 5 + Phase 3.3 verification slice).

* ``/admin/orders``            inbox with status/tier filters + unread badge
                               (Phase 3.3 rows additionally show the live
                               payments.status for the verification queue)
* ``/admin/orders/{id}``       detail: client info, form data, preview iframe,
                               payment proof, actions (confirm / reject / publish)
* ``/admin/payments/{id}/proof``  authenticated-only proof serving — the DB
                               stores only a relative path; the browser never
                               supplies a filesystem path (no arbitrary read)
* ``/admin/api/notifications/unread``  polled every 15 s by the dashboard JS
* ``/admin/orders/{id}/notify-payment``  wa.me link to ADMIN (prefilled report)
* POST confirm-payment / reject / publish / mark-read

Phase 3.3 (admin payment verification) hardening:
* approve/reject are guarded ATOMIC transactions — the payment flip uses a
  conditional UPDATE (WHERE status='submitted') and the order moves through
  the EXISTING state machine in the same transaction; any failure rolls both
  back (no half-verified state);
* amounts are always re-derived from the order row (never admin/browser
  input); idempotency is deterministic: already-final states return 409;
* every action is recorded in ``order_events`` (actor=admin, action,
  from/to status, timestamp) via the existing audit mechanism.

Every status change goes through ``orders.transition()`` (which appends an
``order_events`` row), and each admin action additionally logs a dedicated
event. Publishing derives the slug from the couple names (Gold/Platinum may
override), creates the invitation, and computes expires_at strictly from the
PUBLISH moment via ``services.tiers.expires_at``.
"""
import asyncio
import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote

from aiohttp import web
from markupsafe import Markup

import config
from db import get_conn
from routes.common import render
from services import notify as notify_svc
from services import orders as orders_svc
from services import payments as payments_svc
from services import render as render_svc
from services import tiers as tiers_svc

#: Payment statuses owned by the Phase 3.3 admin verification flow. The
#: customer flow (Phase 3.2) only ever produces 'pending'/'submitted';
#: 'verified'/'rejected' may ONLY be written by guarded admin actions.
PAYMENT_VERIFIED = "verified"
PAYMENT_REJECTED = "rejected"

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

_BADGE_COLORS = {
    orders_svc.DRAFT: "#71717a",
    orders_svc.NEW: "#71717a",
    orders_svc.PENDING_PAYMENT: "#fbbf24",
    orders_svc.PAYMENT_REPORTED: "#38bdf8",
    orders_svc.PAID: "#34d399",
    orders_svc.IN_PROTECTION: "#a78bfa",
    orders_svc.PUBLISHED: "#34d399",
    orders_svc.EXPIRED: "#f87171",
    orders_svc.CANCELLED: "#ef4444",
}


def status_badge(status: str) -> str:
    color = _BADGE_COLORS.get(status, "#a1a1aa")
    label = STATUS_LABELS.get(status, status)
    return render(
        '<span style="background:{{ color }}; color:#000; font-size:9px;'
        ' font-weight:bold; padding:2px 6px; border-radius:4px;">{{ label }}</span>',
        color=color, label=label)


# ---------------------------------------------------------------------------
# Shared queries
# ---------------------------------------------------------------------------

_ORDER_SELECT = '''
    SELECT o.id, o.code, o.status, o.buyer_name, o.whatsapp, o.amount,
           o.discount, o.payment_method, o.payment_unique_code,
           o.payment_deadline, o.published_at, o.expires_at, o.created_at,
           ti.code, ti.name, t.name, t.source_dir, c.name, c.whatsapp,
           o.client_id
    FROM orders o
    JOIN tiers ti ON ti.id = o.tier_id
    LEFT JOIN templates t ON t.id = o.template_id
    LEFT JOIN clients c ON c.id = o.client_id
'''


def _fetch_order(cursor, order_id):
    cursor.execute(_ORDER_SELECT + ' WHERE o.id = ?', (order_id,))
    return cursor.fetchone()


def _invitation_for_order(cursor, order_id):
    cursor.execute(
        'SELECT id, slug, couple_names, content_json FROM invitations'
        ' WHERE order_id = ?', (order_id,))
    return cursor.fetchone()


def _latest_payment_proof(cursor, order_id):
    """Return (payment_id, absolute path or None, method, amount, status).

    Phase 3.3: the verification queue acts on the SUBMITTED attempt — the
    newest payment row still awaiting a decision. Historical verified /
    rejected rows stay visible for the audit trail but are never picked up
    by approve/reject (guarded UPDATEs match on the submitted row id only).
    """
    cursor.execute(
        'SELECT id, method, amount, proof_path, status FROM payments'
        " WHERE order_id = ? AND status = ?"
        ' ORDER BY id DESC LIMIT 1', (order_id, payments_svc.SUBMITTED))
    row = cursor.fetchone()
    if row is None:
        # No pending decision — fall back to the most recent attempt so the
        # admin detail page can still show its proof/reference (read-only).
        cursor.execute(
            'SELECT id, method, amount, proof_path, status FROM payments'
            ' WHERE order_id = ? ORDER BY id DESC LIMIT 1', (order_id,))
        row = cursor.fetchone()
        if row is None:
            return None
    pid, method, amount, proof_path, status = row
    abs_path = security_safe_path(proof_path) if proof_path else None
    cursor.execute('SELECT reference, submitted_at FROM payments WHERE id = ?',
                   (pid,))
    meta_row = cursor.fetchone()
    prov = payments_svc.parse_provenance(
        {"reference": (meta_row[0] if meta_row else "") or ""})
    return {
        "id": pid, "method": method, "amount": amount, "status": status,
        "proof_abs": abs_path,
        "proof_url": f"/admin/payments/{pid}/proof" if proof_path else "",
        # Phase 3.3 detail extras — provenance is decoded server-side; the
        # raw filesystem path is NEVER exposed to the browser.
        "reference": ((meta_row[0] if meta_row else "") or ""),
        "submitted_at": ((meta_row[1] if meta_row else "") or ""),
        "original_name": str(prov.get("original_name") or ""),
        "mime": str(prov.get("mime") or ""),
        "size": int(prov.get("size") or 0),
    }


def security_safe_path(rel_path: str):
    """Resolve a stored relative path inside UPLOAD_DIR, blocking traversal."""
    import security
    return security.safe_upload_path(rel_path or "")


def _redact_reference(raw: str) -> str:
    """Human-safe reference for the detail page (Phase 3.3).

    PROOF:<json> provenance blobs are replaced by the decoded original file
    name; anything else is shown as-is. The stored filesystem path NEVER
    reaches the browser either way.
    """
    raw = str(raw or "")
    prov = payments_svc.parse_provenance({"reference": raw})
    if prov:
        return os.path.basename(str(prov.get("original_name") or ""))[:120]
    return raw[:80]


def _proof_provenance(cursor, payment_id) -> dict:
    """Decode the server-side proof provenance for one payment row (Phase 3.3).

    The browser NEVER supplies a filesystem path: only the payments.proof_path
    recorded by Phase 3.2 is resolved through safe_upload_path() (traversal
    blocked), and the reference column carries the packed PROOF:<json>
    provenance (original name / mime / size). Returns {} when nothing valid
    is stored.
    """
    cursor.execute(
        'SELECT reference FROM payments WHERE id = ?', (payment_id,))
    row = cursor.fetchone()
    if not row:
        return {}
    info = payments_svc.parse_provenance({"reference": row[0]})
    original = os.path.basename(str(info.get("original_name") or ""))[:120]
    return {
        "original_name": original,
        "mime": str(info.get("mime") or ""),
        "size": int(info.get("size") or 0),
    }


def _form_data_summary(cursor, order_id):
    """Client-submitted invitation draft (invitations.content_json) as dict."""
    cursor.execute(
        'SELECT content_json FROM invitations WHERE order_id = ?'
        ' ORDER BY id DESC LIMIT 1', (order_id,))
    row = cursor.fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row[0] or "{}")
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _template_source_dir(cursor, template_id) -> str:
    cursor.execute('SELECT source_dir FROM templates WHERE id = ?', (template_id,))
    row = cursor.fetchone()
    return (row[0] or "") if row else ""


def _tier_allows_custom_slug(tier_code: str) -> bool:
    try:
        return tiers_svc.has_feature(tier_code, "custom_slug")
    except KeyError:
        return False


def public_url_for(slug: str) -> str:
    base = (config.BASE_DOMAIN or "invite.sukamoto.web.id").lstrip(".")
    return f"https://{slug}.{base}"


# ---------------------------------------------------------------------------
# Inbox page
# ---------------------------------------------------------------------------

ORDERS_HTML = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Orders - SUKA MOTO</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: sans-serif; }
        body { background: #09090b; color: #f4f4f5; padding: 16px; }
        .wrap { max-width: 760px; margin: 0 auto; }
        h1 { font-size: 16px; color: #fbbf24; margin-bottom: 4px; }
        .sub { font-size: 11px; color: #a1a1aa; margin-bottom: 14px; }
        .sub a { color: #fbbf24; text-decoration: none; }
        .filters { display: flex; gap: 8px; margin-bottom: 14px; }
        .filters select { padding: 8px; border-radius: 8px; border: 1px solid #3f3f46; background: #18181b; color: #fff; font-size: 12px; }
        .notif-bar { background: #121215; border: 1px solid #27272a; border-radius: 10px; padding: 10px 12px; margin-bottom: 14px; font-size: 11px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .badge-count { background: #ef4444; color: #fff; border-radius: 10px; padding: 1px 7px; font-weight: bold; }
        button.small { background: #27272a; color: #fbbf24; border: none; padding: 5px 9px; border-radius: 6px; font-size: 10px; cursor: pointer; font-weight: bold; }
        table { width: 100%; border-collapse: collapse; font-size: 12px; background: #121215; border: 1px solid #27272a; border-radius: 10px; overflow: hidden; }
        th { text-align: left; padding: 10px; border-bottom: 2px solid #3f3f46; color: #fbbf24; }
        td { padding: 10px; border-bottom: 1px solid #27272a; vertical-align: top; }
        tr:hover td { background: #18181b; }
        a.order-link { color: #fff; text-decoration: none; font-weight: bold; }
        a.wa { color: #34d399; text-decoration: none; font-weight: bold; font-size: 10px; }
        .empty { text-align: center; color: #71717a; padding: 24px; font-size: 12px; }
    </style>
</head>
<body>
<div class="wrap">
    <h1>Inbox Pesanan</h1>
    <div class="sub">
        <a href="/">&larr; Beranda</a> | <a href="/admin">Panel</a> |
        <a href="/admin/logout">Keluar</a>
    </div>

    <div class="notif-bar">
        <span>Notifikasi belum dibaca:
            <span class="badge-count" id="unreadCount">{{ unread_count }}</span></span>
        <button class="small" id="enableNotifBtn" type="button">Enable notifications</button>
        <span id="notifStatus" style="color:#71717a;"></span>
        <span style="flex:1;"></span>
        <span id="lastNotif" style="color:#a1a1aa;"></span>
    </div>

    <form method="GET" action="/admin/orders" class="filters">
        <select name="status">
            <option value="">Semua status</option>
            {% for value, label in status_options %}
            <option value="{{ value }}" {% if value == sel_status %}selected{% endif %}>{{ label }}</option>
            {% endfor %}
        </select>
        <select name="tier">
            <option value="">Semua tier</option>
            {% for code in tier_codes %}
            <option value="{{ code }}" {% if code == sel_tier %}selected{% endif %}>{{ code }}</option>
            {% endfor %}
        </select>
        <button class="small" type="submit">Filter</button>
    </form>

    <table>
        <tr><th>Kode</th><th>Pemesan</th><th>Tier</th><th>Status</th>
            <th>Dibuat</th><th>Aksi</th></tr>
        {{ rows_html }}
    </table>
</div>

<script>
(function () {
    var soundOk = false;
    function ping() {
        // Tiny WebAudio beep — no external asset needed.
        try {
            var ctx = ping.ctx || (ping.ctx = new (window.AudioContext || window.webkitAudioContext)());
            var osc = ctx.createOscillator(), gain = ctx.createGain();
            osc.connect(gain); gain.connect(ctx.destination);
            osc.frequency.value = 880; gain.gain.value = 0.05;
            osc.start(); osc.stop(ctx.currentTime + 0.15);
        } catch (e) { /* silent */ }
    }
    document.getElementById('enableNotifBtn').addEventListener('click', function () {
        if (!('Notification' in window)) {
            document.getElementById('notifStatus').textContent = 'Browser tidak mendukung notifikasi.';
            return;
        }
        Notification.requestPermission().then(function (perm) {
            document.getElementById('notifStatus').textContent = 'Izin: ' + perm;
        });
    });
    var seen = null;
    function poll() {
        fetch('/admin/api/notifications/unread')
            .then(function (r) { return r.json(); })
            .then(function (j) {
                document.getElementById('unreadCount').textContent = j.count;
                var ids = (j.notifications || []).map(function (n) { return n.id; });
                if (seen === null) { seen = ids; return; }
                var fresh = ids.filter(function (id) { return seen.indexOf(id) === -1; });
                if (fresh.length) {
                    ping();
                    var latest = (j.notifications || [])[0] || {};
                    document.getElementById('lastNotif').textContent =
                        latest.kind || '';
                    if ('Notification' in window && Notification.permission === 'granted') {
                        new Notification('SUKA MOTO: ' + (latest.kind || 'notifikasi baru'),
                            { body: latest.summary || '' });
                    }
                }
                seen = ids;
            })
            .catch(function () { /* silent */ });
    }
    poll();
    setInterval(poll, 15000);
})();
</script>
</body>
</html>
"""

ORDER_ROW_HTML = """
<tr>
    <td><a class="order-link" href="/admin/orders/{{ oid }}">{{ ocode }}</a></td>
    <td>{{ buyer }}<br><span style="color:#71717a; font-size:10px;">{{ wha }}</span></td>
    <td>{{ tier_name }}</td>
    <td>{{ badge }}</td>
    {% if pay_status %}<td style="font-size:10px;">{{ pay_status }}</td>{% endif %}
    <td style="color:#a1a1aa; font-size:10px;">{{ created }}</td>
    <td>
        <a class="wa" href="{{ wa_admin_link }}" target="_blank" rel="noopener">WA Laporkan</a>
    </td>
</tr>
"""


def _payment_statuses_by_order(cursor, order_ids):
    """Map order_id -> live payments.status for the listing (Phase 3.3).

    One batched query per page (no N+1). The status shown is the newest
    payment attempt per order — exactly what the verification queue needs
    ('submitted' rows are the ones awaiting approve/reject).
    """
    if not order_ids:
        return {}
    placeholders = ",".join("?" for _ in order_ids)
    cursor.execute(
        f"SELECT order_id, status FROM payments WHERE id IN ("
        f" SELECT MAX(id) FROM payments WHERE order_id IN ({placeholders})"
        " GROUP BY order_id)", tuple(order_ids))
    return {row[0]: row[1] for row in cursor.fetchall()}


async def handle_orders_page(request):
    sel_status = request.query.get('status', '')
    sel_tier = request.query.get('tier', '')

    conn = get_conn()
    cursor = conn.cursor()
    where, params = [], []
    if sel_status:
        where.append("o.status = ?")
        params.append(sel_status)
    if sel_tier:
        where.append("ti.code = ?")
        params.append(sel_tier)
    sql = _ORDER_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.id DESC LIMIT 200"
    cursor.execute(sql, params)
    orders = cursor.fetchall()
    # Phase 3.3: annotate each row with its live payment status so the
    # verification queue (payment_reported + submitted) is scannable.
    pay_map = _payment_statuses_by_order(cursor, [o[0] for o in orders])

    unread = orders_svc.unread_notifications(conn, limit=100)
    conn.close()

    origin = str(request.url.origin())
    rows_html = ""
    for (oid, code, status, buyer, wha, amount, discount, method, puc,
         deadline, published, expires, created, tcode, tname_, tmpl_name,
         source_dir, cname, cwha, client_id) in orders:
        wa_text = notify_svc.order_report(
            {"code": code, "buyer_name": buyer, "tier_name": tname_,
             "template_name": tmpl_name or "-", "total": amount,
             "status": STATUS_LABELS.get(status, status)},
            f"{origin}/admin/orders/{oid}")
        rows_html += render(
            ORDER_ROW_HTML, oid=oid, ocode=code, buyer=buyer or "-",
            wha=wha or "-", tier_name=tname_,
            badge=Markup(status_badge(status)),
            pay_status=pay_map.get(oid, ""),
            created=created or "",
            wa_admin_link=notify_svc.wa_link(config.ADMIN_WHATSAPP, wa_text))
    if not rows_html:
        rows_html = ('<tr><td colspan="6" class="empty">'
                     'Belum ada pesanan.</td></tr>')

    html = render(
        ORDERS_HTML,
        unread_count=len(unread),
        status_options=[(s, STATUS_LABELS[s]) for s in orders_svc.ALL_STATES],
        tier_codes=[t["code"] for t in tiers_svc.TIERS],
        sel_status=sel_status, sel_tier=sel_tier,
        rows_html=Markup(rows_html))
    return web.Response(text=html, content_type='text/html')


# ---------------------------------------------------------------------------
# Notifications API (polled by the pages above)
# ---------------------------------------------------------------------------

async def handle_notifications_unread(request):
    conn = get_conn()
    items = orders_svc.unread_notifications(conn, limit=50)
    conn.close()
    out = []
    for nid, kind, payload, created_at in items:
        summary = "{} {}".format(payload.get("code", ""),
                                 payload.get("buyer_name", "")).strip()
        out.append({"id": nid, "kind": kind, "summary": summary,
                    "created_at": created_at})
    return web.json_response({"count": len(out), "notifications": out})


async def handle_mark_read(request):
    notification_id = request.match_info['id']
    conn = get_conn()
    orders_svc.mark_notification_read(conn, notification_id)
    conn.close()
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Order detail page
# ---------------------------------------------------------------------------

TIMELINE_STEPS = [
    ("created", "Dibuat"),
    ("pending", "Menunggu Pembayaran"),
    ("reported", "Bukti Diterima"),
    ("paid", "Dikonfirmasi"),
    ("published", "Undangan Aktif"),
]

DETAIL_HTML = """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pesanan {{ ocode }} - Admin</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: sans-serif; }
        body { background: #09090b; color: #f4f4f5; padding: 16px; }
        .wrap { max-width: 760px; margin: 0 auto; }
        h1 { font-size: 16px; color: #fbbf24; }
        .sub { font-size: 11px; color: #a1a1aa; margin: 4px 0 14px; }
        .sub a { color: #fbbf24; text-decoration: none; }
        .card { background: #121215; border: 1px solid #27272a; border-radius: 10px; padding: 14px; margin-bottom: 14px; }
        .card h3 { font-size: 12px; color: #fbbf24; margin-bottom: 8px; }
        .kv { display: flex; justify-content: space-between; font-size: 11px; padding: 3px 0; border-bottom: 1px dashed #27272a; }
        .kv:last-child { border-bottom: none; }
        .kv span:first-child { color: #a1a1aa; }
        .timeline { display: flex; gap: 4px; margin: 10px 0; flex-wrap: wrap; }
        .step { flex: 1; min-width: 90px; text-align: center; font-size: 9px; padding: 6px 4px; border-radius: 6px; background: #18181b; color: #71717a; border: 1px solid #27272a; }
        .step.done { background: #14351f; color: #34d399; border-color: #34d399; font-weight: bold; }
        .step.current { background: #3a2f06; color: #fbbf24; border-color: #fbbf24; font-weight: bold; }
        textarea, input[type=text] { width: 100%; padding: 8px; border-radius: 6px; border: 1px solid #3f3f46; background: #18181b; color: #fff; font-size: 11px; margin-top: 6px; }
        button.act { border: none; padding: 9px 12px; border-radius: 8px; font-size: 11px; font-weight: bold; cursor: pointer; margin: 4px 4px 0 0; }
        .btn-green { background: #34d399; color: #000; }
        .btn-red { background: #ef4444; color: #fff; }
        .btn-gold { background: #fbbf24; color: #000; }
        .btn-gray { background: #27272a; color: #fbbf24; }
        iframe.preview { width: 100%; height: 420px; border: 1px solid #27272a; border-radius: 8px; background: #fff; }
        img.proof { max-width: 100%; border-radius: 8px; border: 1px solid #3f3f46; }
        .flash { background: #14351f; color: #34d399; border: 1px solid #34d399; padding: 8px 10px; border-radius: 8px; font-size: 11px; margin-bottom: 12px; }
        .flash.err { background: #3b0d0d; color: #f87171; border-color: #ef4444; }
        .evt { font-size: 10px; color: #a1a1aa; padding: 2px 0; border-bottom: 1px dashed #27272a; }
        .publink { color: #34d399; font-weight: bold; word-break: break-all; }
    </style>
</head>
<body>
<div class="wrap">
    <h1>Pesanan {{ ocode }}</h1>
    <div class="sub">
        <a href="/admin/orders">&larr; Inbox Pesanan</a> |
        <a href="/admin">Panel</a> | <a href="/admin/logout">Keluar</a>
    </div>

    {% if flash %}<div class="flash {% if flash_err %}err{% endif %}">{{ flash }}</div>{% endif %}

    <div class="card">
        <h3>Status</h3>
        <div>{{ badge }}</div>
        <div class="timeline">
            {% for step in timeline %}
            <div class="step {{ step.cls }}">{{ step.label }}</div>
            {% endfor %}
        </div>
        {% if deadline_left %}
        <div style="font-size:10px; color:#fbbf24;">Batas pembayaran tersisa: {{ deadline_left }}</div>
        {% endif %}
    </div>

    <div class="card">
        <h3>Informasi Klien &amp; Pesanan</h3>
        <div class="kv"><span>Nama pemesan</span><strong>{{ buyer }}</strong></div>
        <div class="kv"><span>WhatsApp</span><strong>{{ wha }}</strong></div>
        <div class="kv"><span>Akun klien</span><strong>{{ client_name }} {{ client_wha }}</strong></div>
        <div class="kv"><span>Tier</span><strong>{{ tier_name }} ({{ tier_code }})</strong></div>
        <div class="kv"><span>Template</span><strong>{{ template_name }}</strong></div>
        <div class="kv"><span>Harga</span><strong>{{ amount }}</strong></div>
        <div class="kv"><span>Diskon</span><strong>{{ discount }}</strong></div>
        <div class="kv"><span>Metode</span><strong>{{ pay_method }}</strong></div>
        <div class="kv"><span>Kode unik</span><strong>{{ unique_code }}</strong></div>
        <div class="kv"><span>Masa berlaku</span><strong>{{ active_days }} hari sejak dipublish</strong></div>
        {% if published_url %}
        <div class="kv"><span>Link undangan</span><a class="publink" href="{{ published_url }}" target="_blank" rel="noopener">{{ published_url }}</a></div>
        {% endif %}
    </div>

    {% if proof %}
    <div class="card">
        <h3>Bukti Pembayaran ({{ proof_status }})</h3>
        {% if proof_reference %}
        <div class="kv"><span>Referensi</span><strong>{{ proof_reference }}</strong></div>
        {% endif %}
        {% if proof_submitted_at %}
        <div class="kv"><span>Dikirim</span><strong>{{ proof_submitted_at }}</strong></div>
        {% endif %}
        {% if proof_amount %}
        <div class="kv"><span>Nominal (authoritative)</span><strong>{{ proof_amount }}</strong></div>
        {% endif %}
        {% if proof_img_ok %}
        <img class="proof" src="{{ proof_url }}" alt="Bukti pembayaran">
        {% else %}
        <p style="font-size:11px; color:#71717a;">File bukti tidak ditemukan di server.</p>
        {% endif %}
    </div>
    {% endif %}

    <div class="card">
        <h3>Isi Formulir Klien</h3>
        {% if form_rows %}
            {% for key, value in form_rows %}
            <div class="kv"><span>{{ key }}</span><strong>{{ value }}</strong></div>
            {% endfor %}
        {% else %}
        <p style="font-size:11px; color:#71717a;">Klien belum mengisi formulir.</p>
        {% endif %}
    </div>

    {% if preview_srcdoc %}
    <div class="card">
        <h3>Preview Undangan</h3>
        <iframe class="preview" sandbox="" srcdoc="{{ preview_srcdoc }}"></iframe>
    </div>
    {% endif %}

    <div class="card">
        <h3>Aksi</h3>
        <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <a class="act btn-gray" style="text-decoration:none;" href="{{ wa_admin_link }}" target="_blank" rel="noopener">Kirimi admin laporan WA</a>
        </div>

        {% if can_confirm %}
        <form method="POST" action="/admin/orders/{{ oid }}/confirm-payment" style="margin-top:8px;">
            <button type="submit" class="act btn-green">Konfirmasi Pembayaran</button>
        </form>
        {% endif %}

        {% if can_reject %}
        <form method="POST" action="/admin/orders/{{ oid }}/reject" style="margin-top:8px;">
            <input type="text" name="reason" placeholder="Alasan penolakan (wajib)" required>
            <button type="submit" class="act btn-red">Tolak Pembayaran</button>
        </form>
        {% endif %}

        {% if can_publish %}
        <form method="POST" action="/admin/orders/{{ oid }}/publish" style="margin-top:8px;">
            {% if can_custom_slug %}
            <input type="text" name="slug" placeholder="Slug khusus (opsional, default dari nama mempelai)">
            {% else %}
            <p style="font-size:10px; color:#71717a;">Slug dibuat otomatis dari nama mempelai (tier {{ tier_code }}).</p>
            {% endif %}
            <button type="submit" class="act btn-gold">Publish Undangan</button>
        </form>
        {% endif %}

        {% if published_url %}
        <form method="POST" action="/admin/orders/{{ oid }}/publish" style="display:inline;">
            <input type="hidden" name="regen_client_link" value="1">
        </form>
        <div style="margin-top:8px;">
            <a class="act btn-green" style="text-decoration:none; display:inline-block;" href="{{ wa_client_link }}" target="_blank" rel="noopener">Kirim link ke klien (WA)</a>
        </div>
        {% endif %}
    </div>

    <div class="card">
        <h3>Riwayat Aktivitas</h3>
        {% for evt in events %}
        <div class="evt">{{ evt.created_at }} — <strong>{{ evt.actor }}</strong>: {{ evt.text }}</div>
        {% endfor %}
        {% if not events %}<p style="font-size:11px; color:#71717a;">Belum ada aktivitas tercatat.</p>{% endif %}
    </div>
</div>
</body>
</html>
"""


def _timeline_for(status: str):
    stage_map = {
        orders_svc.DRAFT: 0, orders_svc.NEW: 0,
        orders_svc.PENDING_PAYMENT: 1,
        orders_svc.PAYMENT_REPORTED: 2,
        orders_svc.PAID: 3, orders_svc.IN_PROTECTION: 3,
        orders_svc.PUBLISHED: 4, orders_svc.EXPIRED: 4,
        orders_svc.CANCELLED: -1,
    }
    current = stage_map.get(status, -1)
    steps = []
    for idx, (_key, label) in enumerate(TIMELINE_STEPS):
        cls = ""
        if current >= 0:
            if idx < current:
                cls = "done"
            elif idx == current:
                cls = "current"
        steps.append({"label": label, "cls": cls})
    return steps


def _deadline_left(deadline: str) -> str:
    if not deadline:
        return ""
    try:
        dt = datetime.strptime(str(deadline), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(str(deadline))
        except ValueError:
            return ""
    delta = dt - datetime.now()
    if delta.total_seconds() <= 0:
        return "habis (pesanan akan dibatalkan otomatis)"
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)
    return f"{hours} jam {minutes} menit"


def _render_preview_html(source_dir: str, data: dict, tier_code: str) -> str:
    """Best-effort iframe preview of the client's draft; '' when impossible."""
    if not source_dir:
        return ""
    tpl_dir = source_dir
    if not os.path.isabs(tpl_dir):
        tpl_dir = os.path.join(config.TEMPLATES_HTML_DIR, source_dir)
    try:
        html = render_svc.render_invitation(
            tpl_dir, data=data or None, theme=(data or {}).get("theme"),
            tier_code=tier_code, mode="preview")
    except Exception:
        return ""
    return html


async def handle_order_detail(request):
    order_id = request.match_info['id']
    conn = get_conn()
    cursor = conn.cursor()
    row = _fetch_order(cursor, order_id)
    if row is None:
        conn.close()
        raise web.HTTPNotFound()
    (oid, code, status, buyer, wha, amount, discount, method, puc,
     deadline, published, expires, created, tcode, tname_, tmpl_name,
     source_dir, cname, cwha, client_id) = row

    proof = _latest_payment_proof(cursor, oid)
    form_data = _form_data_summary(cursor, oid)
    inv = _invitation_for_order(cursor, oid)

    cursor.execute(
        'SELECT actor, action, from_status, to_status, note, created_at'
        ' FROM order_events WHERE order_id = ? ORDER BY id', (oid,))
    events = []
    for actor, action, frm, to, note, created_at in cursor.fetchall():
        if frm and to and frm != to:
            text = f"{action} ({frm} → {to})"
        else:
            text = action
        if note:
            text += f" — {note}"
        events.append({"actor": actor, "text": text, "created_at": created_at})
    conn.close()

    origin = str(request.url.origin())
    wa_text = notify_svc.order_report(
        {"code": code, "buyer_name": buyer, "tier_name": tname_,
         "template_name": tmpl_name or "-", "total": amount,
         "status": STATUS_LABELS.get(status, status)},
        f"{origin}/admin/orders/{oid}")

    can_confirm = status == orders_svc.PAYMENT_REPORTED
    can_reject = status == orders_svc.PAYMENT_REPORTED
    can_publish = status == orders_svc.PAID
    can_custom_slug = _tier_allows_custom_slug(tcode)

    published_url = public_url_for(inv[1]) if (inv and inv[1]) else ""
    wa_client_link = ""
    if published_url:
        wa_client_link = notify_svc.wa_link(
            wha or cwha or "",
            notify_svc.client_publish_message(published_url, inv[2] or ""))

    preview_srcdoc = ""
    if source_dir:
        preview_srcdoc = _render_preview_html(source_dir, form_data, tcode)

    form_rows = sorted((k, str(v)) for k, v in form_data.items()
                       if not isinstance(v, (dict, list)))

    flash = request.query.get('msg', '')
    flash_err = request.query.get('err', '') == '1'

    html = render(
        DETAIL_HTML,
        oid=oid, ocode=code,
        badge=Markup(status_badge(status)),
        timeline=_timeline_for(status),
        deadline_left=_deadline_left(deadline) if status == orders_svc.PENDING_PAYMENT else "",
        buyer=buyer or "-", wha=wha or "-",
        client_name=cname or "(guest)", client_wha=cwha or "",
        tier_name=tname_, tier_code=tcode,
        template_name=tmpl_name or "-",
        amount=amount or "-", discount=discount or "-",
        pay_method=method or "-", unique_code=puc or "-",
        active_days=tiers_svc.active_days(tcode),
        published_url=published_url,
        proof=bool(proof),
        proof_status=(proof or {}).get("status", ""),
        proof_url=(proof or {}).get("proof_url", ""),
        proof_img_ok=bool(proof and proof.get("proof_abs")),
        # Phase 3.3: verification metadata (server-derived; no raw paths).
        proof_reference=_redact_reference((proof or {}).get("reference", "")),
        proof_submitted_at=(proof or {}).get("submitted_at", ""),
        proof_amount=(proof or {}).get("amount", ""),
        proof_original_name=(proof or {}).get("original_name", ""),
        form_rows=form_rows,
        preview_srcdoc=preview_srcdoc,
        wa_admin_link=notify_svc.wa_link(config.ADMIN_WHATSAPP, wa_text),
        wa_client_link=wa_client_link,
        can_confirm=can_confirm, can_reject=can_reject,
        can_publish=can_publish, can_custom_slug=can_custom_slug,
        events=events, flash=flash, flash_err=flash_err)
    return web.Response(text=html, content_type='text/html')


async def handle_payment_proof(request):
    """Serve the uploaded proof image — only to authenticated admins.

    Phase 3.3 hardening: the payment row must belong to a REAL order (the
    browser supplies only a numeric id; the path itself comes from the DB),
    so foreign / fabricated ids cannot be used for IDOR-style reads and raw
    filesystem paths are never accepted as input.
    """
    payment_id = request.match_info['id']
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT p.proof_path FROM payments p'
        ' JOIN orders o ON o.id = p.order_id'
        ' WHERE p.id = ?', (payment_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        raise web.HTTPNotFound()
    abs_path = security_safe_path(row[0])
    if not abs_path or not os.path.isfile(abs_path):
        raise web.HTTPNotFound()
    return web.FileResponse(abs_path)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _conflict(order_id, message):
    """Deterministic already-processed / invalid-state exception (Phase 3.3).

    Returns an HTTPConflict *exception* (never raise/return a plain
    Response object — aiohttp requires exceptions to derive from
    BaseException). Handlers `raise` it inside the transaction; the
    `except web.HTTPException` path closes the connection and re-raises,
    producing a proper 409 JSON response. The UI flash mechanism
    (?err=1&msg=...) stays available via X-Flash-Message header without
    breaking the JSON contract.
    """
    exc = web.HTTPConflict(
        text=json.dumps(
            {"ok": False, "error": "invalid_state", "message": message}),
        content_type="application/json")
    exc.headers["X-Flash-Message"] = message
    return exc


def _active_submitted_payment(cursor, order_id):
    """The ACTIVE verification attempt for an order: newest submitted row.

    Phase 3.2 mark_submitted() produces status='submitted'; the legacy
    'pending' guard never matched those rows (silent no-op bug). Rejected /
    verified history rows are ignored here — approve/reject act on the
    submitted attempt only.
    """
    cursor.execute(
        "SELECT id, status FROM payments"
        " WHERE order_id = ? AND status = ?"
        " ORDER BY id DESC LIMIT 1", (order_id, payments_svc.SUBMITTED))
    return cursor.fetchone()


async def handle_confirm_payment(request):
    """APPROVE (Phase 3.3): payment submitted->verified, order
    payment_reported->paid, atomically in ONE transaction.

    Amounts are never read from the browser; state comes from the DB only.
    Any failure (guarded UPDATE rc!=1, illegal transition, commit error)
    rolls back payment + order + audit event together.
    """
    order_id = request.match_info['id']
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        row = _fetch_order(cursor, order_id)
        if row is None:
            conn.rollback()
            raise web.HTTPNotFound()
        oid, code, status, amount = row[0], row[1], row[2], row[5]

        if status != orders_svc.PAYMENT_REPORTED:
            conn.rollback()
            raise _conflict(
                oid, 'Hanya pesanan "Bukti Diterima" yang bisa dikonfirmasi.')

        pay = _active_submitted_payment(cursor, oid)
        if pay is None:
            conn.rollback()
            raise _conflict(oid, 'Tidak ada pembayaran submitted untuk diverifikasi.')

        now = orders_svc._now_str()
        cursor.execute(
            "UPDATE payments SET status = ?, verified_at = ?, updated_at = ?"
            " WHERE id = ? AND status = ?",
            (PAYMENT_VERIFIED, now, now, pay[0], payments_svc.SUBMITTED))
        if cursor.rowcount != 1:
            # Lost race against another admin decision — deterministic stop.
            conn.rollback()
            raise _conflict(oid, 'Pembayaran sudah diproses (already verified).')

        orders_svc.transition(conn, oid, orders_svc.PAID, actor="admin",
                              actor_note="pembayaran dikonfirmasi", commit=False)
        orders_svc.log_order_event(conn, oid, actor="admin",
                                   action="approve", from_status=status,
                                   to_status=orders_svc.PAID,
                                   note=f"amount={amount}", commit=False)
        cursor.execute(
            'UPDATE admin_notifications SET read = 1'
            " WHERE kind = 'payment_reported'"
            " AND payload_json LIKE ?", (f'%{code}%',))
        conn.commit()
    except web.HTTPException:
        conn.close()
        raise
    except Exception:
        conn.rollback()
        conn.close()
        raise web.HTTPInternalServerError(
            text=json.dumps({"ok": False, "error": "server_error",
                             "message": "Gagal mengonfirmasi pembayaran."}),
            content_type="application/json")
    conn.close()
    raise web.HTTPFound(f'/admin/orders/{oid}?msg=' +
                        quote('Pembayaran dikonfirmasi. Silakan publish undangan.'))


async def handle_reject_payment(request):
    """REJECT (Phase 3.3): payment submitted->rejected, order
    payment_reported->pending_payment (customer may re-submit a new proof
    through the unchanged Phase 3.2 flow), atomically in ONE transaction."""
    order_id = request.match_info['id']
    data = await request.post()
    reason = (data.get('reason') or '').strip()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        row = _fetch_order(cursor, order_id)
        if row is None:
            conn.rollback()
            raise web.HTTPNotFound()
        oid, status = row[0], row[2]

        if not reason:
            conn.rollback()
            raise _conflict(oid, 'Alasan penolakan wajib diisi.')
        if status != orders_svc.PAYMENT_REPORTED:
            conn.rollback()
            raise _conflict(oid, 'Pesanan tidak berada pada status Bukti Diterima.')

        pay = _active_submitted_payment(cursor, oid)
        if pay is None:
            conn.rollback()
            raise _conflict(oid, 'Tidak ada pembayaran submitted untuk ditolak.')

        now = orders_svc._now_str()
        cursor.execute(
            "UPDATE payments SET status = ?, updated_at = ?"
            " WHERE id = ? AND status = ?",
            (PAYMENT_REJECTED, now, pay[0], payments_svc.SUBMITTED))
        if cursor.rowcount != 1:
            conn.rollback()
            raise _conflict(oid, 'Pembayaran sudah diproses (already rejected).')

        # payment_reported -> pending_payment (existing state-machine edge)
        orders_svc.transition(conn, oid, orders_svc.PENDING_PAYMENT,
                              actor="admin", actor_note=f"penolakan: {reason}",
                              commit=False)
        orders_svc.log_order_event(conn, oid, actor="admin",
                                   action="reject", from_status=status,
                                   to_status=orders_svc.PENDING_PAYMENT,
                                   note=reason, commit=False)
        conn.commit()
    except web.HTTPException:
        conn.close()
        raise
    except Exception:
        conn.rollback()
        conn.close()
        raise web.HTTPInternalServerError(
            text=json.dumps({"ok": False, "error": "server_error",
                             "message": "Gagal menolak pembayaran."}),
            content_type="application/json")
    conn.close()
    raise web.HTTPFound(f'/admin/orders/{oid}?msg=' +
                        quote(f"Pembayaran ditolak: {reason}"))


async def handle_publish(request):
    order_id = request.match_info['id']
    data = await request.post()
    conn = get_conn()
    cursor = conn.cursor()
    row = _fetch_order(cursor, order_id)
    if row is None:
        conn.close()
        raise web.HTTPNotFound()
    (oid, code, status, buyer, wha, amount, discount, method, puc,
     deadline, published, expires, created, tcode, tname_, tmpl_name,
     source_dir, cname, cwha, client_id) = row

    inv = _invitation_for_order(cursor, oid)
    if data.get('regen_client_link') and inv and inv[1]:
        conn.close()
        url = public_url_for(inv[1])
        raise web.HTTPFound(f'/admin/orders/{oid}?msg=' +
                            quote(f'Link aktif: {url}'))

    if status != orders_svc.PAID:
        conn.close()
        raise web.HTTPFound(f'/admin/orders/{oid}?err=1&msg=' +
                            quote('Pesanan harus berstatus Dikonfirmasi sebelum publish.'))

    form_data = _form_data_summary(cursor, oid)
    couple_names = (form_data.get("couple_names") or form_data.get("names")
                    or form_data.get("mempelai") or buyer or "Mempelai")

    allow_custom = _tier_allows_custom_slug(tcode)
    custom = (data.get('slug') or '').strip() if allow_custom else ''
    slug = orders_svc.generate_slug(conn,
                                    couple_names,
                                    allow_custom=allow_custom, custom=custom)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expires_dt = tiers_svc.expires_at(tcode, now)  # duration from PUBLISH moment
    expires_str = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

    if inv:
        cursor.execute(
            'UPDATE invitations SET slug = ?, couple_names = ?, state = ?,'
            ' published_at = ?, expires_at = ?, updated_at = ?,'
            ' content_json = COALESCE(NULLIF(content_json, \'{}\'), ?)'
            ' WHERE id = ?',
            (slug, couple_names, "published", now, expires_str, now,
             json.dumps(form_data, ensure_ascii=False), inv[0]))
    else:
        cursor.execute(
            'INSERT INTO invitations (order_id, slug, title, couple_names,'
            ' event_date, event_time, venue_name, venue_address, content_json,'
            ' state, published_at, expires_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (oid, slug, f"The Wedding of {couple_names}", couple_names,
             form_data.get("date", ""), form_data.get("time", ""),
             form_data.get("venue", ""), form_data.get("address", ""),
             json.dumps(form_data, ensure_ascii=False),
             "published", now, expires_str))
    conn.commit()

    orders_svc.transition(conn, oid, orders_svc.PUBLISHED,
                          actor="admin", actor_note=f"published slug={slug}")
    orders_svc.notify_admin(conn, "order_published", {
        "code": code, "slug": slug, "buyer_name": buyer,
        "tier_code": tcode, "url": public_url_for(slug)})
    orders_svc.log_order_event(conn, oid, actor="admin", action="published",
                               from_status=status, to_status=orders_svc.PUBLISHED,
                               note=slug)
    # Fire-and-forget Telegram when configured (silent no-op otherwise).
    try:
        request.app["telegram_tasks"] = request.app.get(
            "telegram_tasks", [])
        task = asyncio.ensure_future(notify_svc.send_telegram(
            notify_svc.order_report(
                {"code": code, "buyer_name": buyer, "tier_name": tname_,
                 "template_name": tmpl_name or "-", "total": amount,
                 "status": "PUBLISHED"},
                public_url_for(slug))))
        request.app["telegram_tasks"].append(task)
    except RuntimeError:
        pass
    conn.close()

    url = public_url_for(slug)
    raise web.HTTPFound(f'/admin/orders/{oid}?msg=' +
                        quote(f'Undangan aktif: {url}'))



def setup(app):
    app.router.add_get('/admin/orders', handle_orders_page)
    app.router.add_get('/admin/orders/{id}', handle_order_detail)
    app.router.add_get('/admin/payments/{id}/proof', handle_payment_proof)
    app.router.add_get('/admin/api/notifications/unread',
                       handle_notifications_unread)
    app.router.add_post('/admin/notifications/{id}/read', handle_mark_read)
    app.router.add_post('/admin/orders/{id}/confirm-payment',
                        handle_confirm_payment)
    app.router.add_post('/admin/orders/{id}/reject', handle_reject_payment)
    app.router.add_post('/admin/orders/{id}/publish', handle_publish)
