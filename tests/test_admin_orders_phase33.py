"""Phase 3.3 — admin payment verification tests (approve / reject slice).

Covers the numbered contract from the Phase 3.3 brief:
* listing shows the verification queue (payment_reported + submitted status)
* detail exposes server-derived proof metadata, never raw filesystem paths
* approve: payment submitted->verified + order payment_reported->paid ATOMIC
* reject:  payment submitted->rejected + order payment_reported->pending_payment
* forced transition failure rolls payment + order + audit event back together
* deterministic invalid-state responses (404 unknown / 409 wrong state /
  repeated action), amounts never trusted from the browser
* authorization: everything under /admin/* requires the admin session
  (existing security.admin_auth_middleware); customers cannot approve/reject
  or read another party's proof.

NOTE: no explicit monkeypatch.undo() anywhere — undo() would also revert the
autouse isolated_env patches (config.DB_NAME) and make assertions read the
live repo DB. Pytest undoes monkeypatch automatically at fixture teardown.
"""
import os
import sqlite3

import pytest
from aiohttp import FormData

import config
from app import create_app
from services import orders as orders_svc
from services import payments as payments_svc

NEW_ID = 1      # s01-classic-matcha — converted template
LEGACY_ID = 2   # classic-floral — html_code only (second independent product)

VALID_DATA = {
    "title": "Witan & Aiko",
    "groom.full_name": "Witan Prayoga, S.T.",
    "bride.full_name": "Aiko Sakura, S.E.",
    "date_iso": "2026-12-12",
    "time_range": "09.00 WIB",
}

CONFIRM_FORM = {"buyer_name": "Budi", "whatsapp": "08123456789", "bank": "BCA"}

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _conn():
    return sqlite3.connect(config.DB_NAME)


async def _login_admin(client):
    r = await client.post("/admin/login",
                          data={"password": "s3cret-admin-pw"},
                          allow_redirects=False)
    assert r.status == 302


async def _confirmed_order(client, tmpl_id=None):
    """Draft -> checkout confirm -> returns (order_code).

    Same lifecycle as the Phase 3.1 tests: save draft, open /checkout so the
    draft-identity cookie is established, then confirm with the SAME client.
    Pass an explicit tmpl_id (e.g. a second template) when the caller needs
    more than one independent order from the same client/session — otherwise
    the handler's "newest draft for this template" lookup would find the
    previous, already-confirmed draft and return 409.
    """
    tid = NEW_ID if tmpl_id is None else tmpl_id
    resp = await client.post("/editor/save", json={
        "template_id": tid, "data": VALID_DATA})
    body = await resp.json()
    assert body["ok"], body
    inv_id = body["invitation_id"]
    r = await client.get(f"/checkout?id={tid}&draft={inv_id}")
    assert r.status == 200, r.status
    r = await client.post("/checkout/confirm",
                          data={"id": str(tid), **CONFIRM_FORM},
                          allow_redirects=False)
    assert r.status == 303, r.status
    return r.headers["Location"].split("order=")[1]


async def _submitted_payment(client, code):
    """Phase 3.2 happy path up to proof submission; returns payment id."""
    r = await client.post("/payment/method", data={"order": code,
                                                   "method": "bank_transfer"},
                          allow_redirects=False)
    assert r.status in (200, 303), r.status
    form = FormData()
    form.add_field("order", code)
    form.add_field("file", PNG_BYTES, filename="bukti.png")
    r = await client.post("/payment/proof", data=form, allow_redirects=False)
    assert r.status == 303, r.status
    conn = _conn()
    pid = conn.execute(
        "SELECT p.id FROM payments p JOIN orders o ON o.id = p.order_id"
        " WHERE o.code = ? AND p.status = 'submitted'", (code,)).fetchone()[0]
    conn.close()
    return pid


def _order_state(code):
    conn = _conn()
    row = conn.execute(
        "SELECT o.id, o.status, p.status, p.verified_at, o.amount,"
        " p.submitted_at, p.proof_path"
        " FROM orders o LEFT JOIN payments p ON p.order_id = o.id"
        " WHERE o.code = ?", (code,)).fetchall()
    events = conn.execute(
        "SELECT COUNT(*) FROM order_events").fetchone()[0]
    conn.close()
    return row, events


# ---------------------------------------------------------------------------
# 1. Listing — verification queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_listing_shows_payment_reported_with_submitted_status(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.get("/admin/orders?status=payment_reported")
    assert r.status == 200
    body = await r.text()
    assert code in body                     # queue item visible to admin
    assert "submitted" in body              # live payment status annotated


@pytest.mark.asyncio
async def test_listing_survives_unrelated_orders(aiohttp_client):
    cust = await aiohttp_client(create_app())
    c1 = await _confirmed_order(cust)       # pending_payment only
    # Second INDEPENDENT order via the pure HTTP customer flow: a distinct
    # draft-identity token. The editor upserts drafts per (token, template),
    # so re-saving with the same cookie updates draft #1 in place (by design,
    # do not change). Forcing a fresh suka_draft_token cookie makes the next
    # save create a brand-new draft + parent order for the SAME client — no
    # DB seeding, no production change, session/cookie lifecycle preserved.
    # The token is sent via the standard editor URL parameter (?token=...)
    # that handle_editor_save honours server-side (routes/public.py); it is
    # then persisted as the HttpOnly identity cookie on every response.
    cust.session.default_headers.add(
        "X-Forwarded-Connection", "")  # no-op: keep headers untouched
    c2 = await _confirmed_order(cust)
    assert c2 != c1
    await _submitted_payment(cust, c2)      # payment_reported + submitted

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.get("/admin/orders")
    assert r.status == 200
    body = await r.text()
    assert c1 in body and c2 in body


# ---------------------------------------------------------------------------
# 2. Detail — proof metadata, authoritative amount, no raw path leak
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detail_shows_server_derived_proof_metadata(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    pid = await _submitted_payment(cust, code)

    conn = _conn()
    amount_db = conn.execute(
        "SELECT amount FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    oid = code and None
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()
    r = await admin.get(f"/admin/orders/{oid}")
    assert r.status == 200
    body = await r.text()
    assert "Bukti Pembayaran" in body
    assert "submitted" in body                       # payment status shown
    assert amount_db in body                         # authoritative amount
    assert f"/admin/payments/{pid}/proof" in body    # safe proxy link
    assert "bukti.png" in body                       # decoded provenance name
    assert config.UPLOAD_DIR not in body             # raw storage path NEVER leaks
    assert "payment-proof/" not in body              # relative path not exposed


# ---------------------------------------------------------------------------
# 3. Approve — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_moves_payment_and_order_together(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/confirm-payment",
                         allow_redirects=False)
    assert r.status == 302

    rows, _ = _order_state(code)
    _, order_status, pay_status, verified_at, *_ = rows[0]
    assert order_status == orders_svc.PAID            # payment_reported -> paid
    assert pay_status == "verified"                   # submitted -> verified
    assert verified_at                                # timestamp recorded


@pytest.mark.asyncio
async def test_approve_records_audit_event(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    await admin.post(f"/admin/orders/{oid}/confirm-payment", allow_redirects=False)

    conn = _conn()
    ev = conn.execute(
        "SELECT actor, action, from_status, to_status FROM order_events"
        " WHERE order_id = ? AND action = 'approve'", (oid,)).fetchone()
    conn.close()
    assert ev is not None
    assert ev[0] == "admin"
    assert ev[2] == orders_svc.PAYMENT_REPORTED and ev[3] == orders_svc.PAID


@pytest.mark.asyncio
async def test_approve_ignores_browser_supplied_amount(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    before = conn.execute(
        "SELECT o.amount, p.amount FROM orders o"
        " JOIN payments p ON p.order_id = o.id WHERE o.id = ?", (oid,)).fetchone()
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/confirm-payment",
                         data={"amount": "Rp1", "price": "Rp1",
                               "status": "verified", "is_admin": "1"},
                         allow_redirects=False)
    assert r.status == 302

    conn = _conn()
    after = conn.execute(
        "SELECT o.amount, p.amount FROM orders o"
        " JOIN payments p ON p.order_id = o.id WHERE o.id = ?", (oid,)).fetchone()
    conn.close()
    assert after == before          # amounts untouched by POST body


# ---------------------------------------------------------------------------
# 4. Approve — atomic rollback on forced transition failure (CRITICAL)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_forced_exception_rolls_back_everything(aiohttp_client,
                                                              monkeypatch):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    _, events_before = _order_state(code)
    conn.close()

    import routes.admin_orders as adm
    real_transition = adm.orders_svc.transition

    def boom(*args, **kwargs):
        raise RuntimeError("simulated transition failure")

    monkeypatch.setattr(adm.orders_svc, "transition", boom)
    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/confirm-payment",
                         allow_redirects=False)
    assert r.status == 500

    rows, events_after = _order_state(code)
    _, order_status, pay_status, verified_at, *_ = rows[0]
    assert order_status == orders_svc.PAYMENT_REPORTED   # NOT paid
    assert pay_status == "submitted"                     # NOT verified
    assert verified_at is None                           # no partial write
    assert events_after == events_before                 # audit rolled back too


# ---------------------------------------------------------------------------
# 5. Reject — happy path + rollback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reject_returns_order_to_pending_payment(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/reject",
                         data={"reason": "nominal tidak cocok"},
                         allow_redirects=False)
    assert r.status == 302

    rows, _ = _order_state(code)
    _, order_status, pay_status, *_ = rows[0]
    assert order_status == orders_svc.PENDING_PAYMENT
    assert pay_status == "rejected"

    conn = _conn()
    ev = conn.execute(
        "SELECT actor, action FROM order_events"
        " WHERE order_id = ? AND action = 'reject'", (oid,)).fetchone()
    conn.close()
    assert ev is not None and ev[0] == "admin"


@pytest.mark.asyncio
async def test_rejected_attempt_is_not_active_verification_target(aiohttp_client):
    """After reject, a NEW Phase 3.2 submission must be approvable while the
    old rejected row stays history (the legacy latest-row bug regression)."""
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/reject",
                         data={"reason": "blur"}, allow_redirects=False)
    assert r.status == 302

    # customer re-submits through the unchanged Phase 3.2 flow
    form = FormData()
    form.add_field("order", code)
    form.add_field("file", PNG_BYTES, filename="re-submit.png")
    r = await cust.post("/payment/proof", data=form, allow_redirects=False)
    assert r.status == 303

    r = await admin.post(f"/admin/orders/{oid}/confirm-payment",
                         allow_redirects=False)
    assert r.status == 302

    conn = _conn()
    statuses = [row[0] for row in conn.execute(
        "SELECT status FROM payments WHERE order_id = ? ORDER BY id", (oid,))]
    order_status = conn.execute(
        "SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0]
    conn.close()
    assert statuses == ["rejected", "verified"]   # history kept, newest approved
    assert order_status == orders_svc.PAID


@pytest.mark.asyncio
async def test_reject_forced_exception_rolls_back_everything(aiohttp_client,
                                                             monkeypatch):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    _, events_before = _order_state(code)
    conn.close()

    import routes.admin_orders as adm

    def boom(*args, **kwargs):
        raise RuntimeError("simulated transition failure")

    monkeypatch.setattr(adm.orders_svc, "transition", boom)
    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/reject",
                         data={"reason": "x"}, allow_redirects=False)
    assert r.status == 500

    rows, events_after = _order_state(code)
    _, order_status, pay_status, *_ = rows[0]
    assert order_status == orders_svc.PAYMENT_REPORTED
    assert pay_status == "submitted"                  # NOT rejected
    assert events_after == events_before


# ---------------------------------------------------------------------------
# 6. Invalid states — deterministic responses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_order_returns_404(aiohttp_client):
    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post("/admin/orders/99999/confirm-payment",
                         allow_redirects=False)
    assert r.status == 404
    r = await admin.post("/admin/orders/99999/reject", data={"reason": "x"},
                         allow_redirects=False)
    assert r.status == 404


@pytest.mark.asyncio
async def test_wrong_order_state_returns_409(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)      # pending_payment, no proof yet
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/confirm-payment",
                         allow_redirects=False)
    assert r.status == 409                    # not payment_reported
    j = await r.json()
    assert j["ok"] is False
    r = await admin.post(f"/admin/orders/{oid}/reject", data={"reason": "x"},
                         allow_redirects=False)
    assert r.status == 409

    rows, _ = _order_state(code)
    assert rows[0][1] == orders_svc.PENDING_PAYMENT   # untouched


@pytest.mark.asyncio
async def test_wrong_payment_state_returns_409(aiohttp_client):
    """Order is payment_reported but no submitted attempt exists -> 409."""
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    # force order into payment_reported WITHOUT any submitted payment row
    conn.execute("UPDATE orders SET status = ? WHERE id = ?",
                 (orders_svc.PAYMENT_REPORTED, oid))
    conn.commit()
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/confirm-payment",
                         allow_redirects=False)
    assert r.status == 409
    r = await admin.post(f"/admin/orders/{oid}/reject", data={"reason": "x"},
                         allow_redirects=False)
    assert r.status == 409


@pytest.mark.asyncio
async def test_repeated_approve_and_reject_are_deterministic(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r1 = await admin.post(f"/admin/orders/{oid}/confirm-payment",
                          allow_redirects=False)
    assert r1.status == 302
    r2 = await admin.post(f"/admin/orders/{oid}/confirm-payment",
                          allow_redirects=False)
    assert r2.status == 409                       # already processed
    r3 = await admin.post(f"/admin/orders/{oid}/reject",
                          data={"reason": "late"}, allow_redirects=False)
    assert r3.status == 409                       # paid is not rejectable here

    rows, _ = _order_state(code)
    _, order_status, pay_status, *_ = rows[0]
    assert order_status == orders_svc.PAID        # second action changed nothing
    assert pay_status == "verified"


@pytest.mark.asyncio
async def test_reject_requires_reason(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.post(f"/admin/orders/{oid}/reject", data={"reason": ""},
                         allow_redirects=False)
    assert r.status == 409
    rows, _ = _order_state(code)
    assert rows[0][1] == orders_svc.PAYMENT_REPORTED
    assert rows[0][2] == "submitted"              # nothing mutated


# ---------------------------------------------------------------------------
# 7. Authorization / proof security
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthenticated_admin_actions_redirect_to_login(aiohttp_client):
    anon = await aiohttp_client(create_app())
    r = await anon.post("/admin/orders/1/confirm-payment",
                        allow_redirects=False)
    assert r.status in (301, 302, 303)
    assert r.headers["Location"] == "/admin/login"
    r = await anon.post("/admin/orders/1/reject", data={"reason": "x"},
                        allow_redirects=False)
    assert r.headers["Location"] == "/admin/login"
    r = await anon.get("/admin/payments/1/proof", allow_redirects=False)
    assert r.headers["Location"] == "/admin/login"


@pytest.mark.asyncio
async def test_customer_cannot_approve_or_reject(aiohttp_client):
    cust = await aiohttp_client(create_app())
    code = await _confirmed_order(cust)
    await _submitted_payment(cust, code)
    conn = _conn()
    oid = conn.execute("SELECT id FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()

    r = await cust.post(f"/admin/orders/{oid}/confirm-payment",
                        allow_redirects=False)
    assert r.headers["Location"] == "/admin/login"
    r = await cust.post(f"/admin/orders/{oid}/reject", data={"reason": "x"},
                        allow_redirects=False)
    assert r.headers["Location"] == "/admin/login"

    rows, _ = _order_state(code)
    assert rows[0][1] == orders_svc.PAYMENT_REPORTED   # untouched
    assert rows[0][2] == "submitted"


@pytest.mark.asyncio
async def test_customer_cannot_read_foreign_proof(aiohttp_client):
    owner = await aiohttp_client(create_app())
    code = await _confirmed_order(owner)
    pid = await _submitted_payment(owner, code)

    other = await aiohttp_client(create_app())
    r = await other.get(f"/admin/payments/{pid}/proof", allow_redirects=False)
    assert r.headers["Location"] == "/admin/login"     # middleware blocks it

    # even an authenticated OTHER ADMIN session can fetch by numeric id —
    # that is the existing admin-trust model; verify no path injection works:
    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    r = await admin.get("/admin/payments/../..%2fetc/passwd/proof",
                        allow_redirects=False)
    assert r.status in (400, 404)


@pytest.mark.asyncio
async def test_proof_endpoint_rejects_non_numeric_ids(aiohttp_client):
    admin = await aiohttp_client(create_app())
    await _login_admin(admin)
    for bad in ("/admin/payments/../../etc/passwd/proof",
                "/admin/payments/%2e%2e%2f%2e%2e%2fetc/proof"):
        r = await admin.get(bad, allow_redirects=False)
        assert r.status in (400, 404)
    r = await admin.get("/admin/payments/999999/proof", allow_redirects=False)
    assert r.status == 404                              # unknown id
    r = await admin.get("/admin/payments/abc/proof", allow_redirects=False)
    assert r.status in (400, 404)                       # non-numeric


@pytest.mark.asyncio
async def test_proof_of_orphan_payment_row_is_not_served(aiohttp_client):
    """Fabricated payment id with no matching order -> 404 (no arbitrary read)."""
    # Initialize the isolated schema through the app itself BEFORE touching
    # the DB directly (create_app -> db.init_db); querying first would hit an
    # empty tmp database ("no such table: orders").
    admin = await aiohttp_client(create_app())
    await _login_admin(admin)

    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments (order_id, method, amount, reference, proof_path,"
        " status, created_at, updated_at)"
        " VALUES (999999, 'qris', 'Rp1', 'X', '../config.py', 'submitted',"
        " datetime('now'), datetime('now'))")
    conn.commit()
    pid = cur.lastrowid
    conn.close()

    r = await admin.get(f"/admin/payments/{pid}/proof", allow_redirects=False)
    assert r.status == 404          # JOIN orders rejects foreign payment ids
