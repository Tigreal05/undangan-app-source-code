"""Phase 3.1 — checkout + order lifecycle tests (A–O).

Covers:
* /checkout opens a valid draft owned by the current editing context
* ownership enforcement (no IDOR via URL id manipulation)
* checkout summary shows ACTUAL invitation data + server-side pricing
* POST /checkout/confirm moves the parent draft order to pending_payment
  using the existing state machine, inside one transaction
* repeated confirm is idempotent (no duplicate orders)
* client-supplied price/status/template cannot manipulate the order
* GET /checkout/status renders for the owner and 403s for others
* legacy checkout flow (no draft) keeps working
"""
import sqlite3

import pytest

import config
from app import create_app

NEW_ID = 1      # s01-classic-matcha — converted template
LEGACY_ID = 2   # classic-floral — html_code only

VALID_DATA = {
    "title": "Witan & Aiko",
    "groom.full_name": "Witan Prayoga, S.T.",
    "bride.full_name": "Aiko Sakura, S.E.",
    "date_iso": "2026-12-12",
    "time_range": "09.00 WIB",
    "events": [{"label": "Akad Nikah", "btn_class": "btn-matcha"}],
}

CONFIRM_FORM = {"buyer_name": "Budi", "whatsapp": "08123456789", "bank": "BCA"}


def _conn():
    return sqlite3.connect(config.DB_NAME)


async def _save_draft(client, data=None):
    resp = await client.post("/editor/save", json={
        "template_id": NEW_ID, "data": data or VALID_DATA})
    body = await resp.json()
    assert body["ok"], body
    return body["invitation_id"]


# ---------------------------------------------------------------------------
# A + D: checkout opens a valid draft and shows its actual data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkout_opens_valid_draft(aiohttp_client):
    client = await aiohttp_client(create_app())
    inv_id = await _save_draft(client)
    resp = await client.get(f"/checkout?id={NEW_ID}&draft={inv_id}")
    assert resp.status == 200
    body = await resp.text()
    # Actual invitation data from the DB appears in the summary…
    assert "Witan &amp; Aiko" in body
    # …and the form now targets the real confirmation endpoint.
    assert 'action="/checkout/confirm"' in body
    assert "Konfirmasi Pesanan" in body


# ---------------------------------------------------------------------------
# B: checkout rejects drafts without ownership
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkout_rejects_foreign_draft(aiohttp_client):
    owner = await aiohttp_client(create_app())
    inv_id = await _save_draft(owner)

    other = await aiohttp_client(create_app())     # no cookie → new identity
    resp = await other.get(f"/checkout?id={NEW_ID}&draft={inv_id}")
    assert resp.status == 403
    j = await resp.json()
    assert j["ok"] is False


@pytest.mark.asyncio
async def test_checkout_rejects_wrong_template(aiohttp_client):
    """Draft id echoed with a mismatched template id must be rejected."""
    client = await aiohttp_client(create_app())
    inv_id = await _save_draft(client)
    resp = await client.get(f"/checkout?id={LEGACY_ID}&draft={inv_id}")
    assert resp.status == 403


# ---------------------------------------------------------------------------
# C: nonexistent invitation / invalid params
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkout_nonexistent_draft_and_bad_params(aiohttp_client):
    client = await aiohttp_client(create_app())
    resp = await client.get(f"/checkout?id={NEW_ID}&draft=99999")
    assert resp.status == 403                       # not found ≠ hijack path
    resp = await client.get("/checkout?id=99999&draft=1")
    assert resp.status == 404                       # unknown template
    resp = await client.get(f"/checkout?id={NEW_ID}&draft=abc")
    assert resp.status == 200                       # non-numeric draft → legacy view


# ---------------------------------------------------------------------------
# E + K + L: pricing comes from the server, never from the client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_is_server_truth(aiohttp_client):
    client = await aiohttp_client(create_app())
    inv_id = await _save_draft(client)

    conn = _conn()
    row = conn.execute(
        "SELECT t.price, t.discount FROM templates t WHERE t.id = ?",
        (NEW_ID,)).fetchone()
    conn.close()
    from services import orders as orders_svc

    # Checkout page total is computed server-side (Rp format, includes code).
    resp = await client.get(f"/checkout?id={NEW_ID}&draft={inv_id}")
    body = await resp.text()
    assert "Total Pembayaran" in body
    assert "Rp " in body
    # No hidden price/status inputs exist that a client could tamper with.
    assert 'name="amount"' not in body
    assert 'name="price"' not in body
    assert 'name="status"' not in body

    # Confirm with an EXTRA forged amount field — it must be ignored.
    resp = await client.post("/checkout/confirm", data={
        "id": str(NEW_ID), "amount": "Rp 1", "status": "paid",
        "tier_id": "99", **CONFIRM_FORM}, allow_redirects=False)
    assert resp.status == 303
    conn = _conn()
    order = conn.execute(
        "SELECT status, amount, payment_unique_code FROM orders").fetchone()
    conn.close()
    assert order[0] == "pending_payment"            # forged status ignored
    stored = orders_svc.parse_price(order[1])
    puc = int(order[2])
    # Stored amount == DB price (+/- discount) + unique code — NOT "Rp 1".
    assert stored != 1                              # forged amount ignored
    assert stored == orders_svc.compute_total(row[0], row[1], order[2])


# ---------------------------------------------------------------------------
# F + G + H: confirm creates the order lifecycle correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_moves_order_to_pending_payment(aiohttp_client):
    client = await aiohttp_client(create_app())
    inv_id = await _save_draft(client)
    resp = await client.get(f"/checkout?id={NEW_ID}&draft={inv_id}")
    assert resp.status == 200

    resp = await client.post("/checkout/confirm",
                             data={"id": str(NEW_ID), **CONFIRM_FORM},
                             allow_redirects=False)
    assert resp.status == 303
    loc = resp.headers["Location"]
    assert loc.startswith("/checkout/status?order=SM-")

    conn = _conn()
    order = conn.execute(
        "SELECT status, buyer_name, whatsapp, payment_method,"
        " payment_unique_code, payment_deadline, amount FROM orders").fetchall()
    inv = conn.execute(
        "SELECT order_id, state FROM invitations WHERE id = ?", (inv_id,)).fetchone()
    events = conn.execute(
        "SELECT action, from_status, to_status FROM order_events ORDER BY id").fetchall()
    conn.close()

    assert len(order) == 1
    status, buyer, wa, method, puc, deadline, amount = order[0]
    assert status == "pending_payment"              # existing state-machine name
    assert buyer == "Budi" and wa == "08123456789"
    assert method == "BCA"
    assert puc and len(puc) == 3 and puc.isdigit()  # existing unique-code system
    assert deadline and amount.startswith("Rp ")
    assert inv[1] == "draft"                        # NOT published here
    assert ("transition:pending_payment", "draft", "pending_payment") in events


# ---------------------------------------------------------------------------
# I: repeated confirm does not create duplicate orders
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeated_confirm_is_idempotent(aiohttp_client):
    client = await aiohttp_client(create_app())
    inv_id = await _save_draft(client)
    first = await client.post("/checkout/confirm",
                              data={"id": str(NEW_ID), **CONFIRM_FORM},
                              allow_redirects=False)
    assert first.status == 303
    code = first.headers["Location"].split("order=")[1]

    second = await client.post("/checkout/confirm",
                               data={"id": str(NEW_ID), **CONFIRM_FORM})
    assert second.status == 200                     # meta-refresh to same order
    body = await second.text()
    assert code in body

    conn = _conn()
    n_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    n_inv = conn.execute("SELECT COUNT(*) FROM invitations").fetchone()[0]
    codes = conn.execute("SELECT code FROM orders").fetchall()
    conn.close()
    assert n_orders == 1 and n_inv == 1
    assert codes == [(code,)]


# ---------------------------------------------------------------------------
# J: invalid payloads are rejected safely (no DB mutation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_confirm_payloads(aiohttp_client):
    client = await aiohttp_client(create_app())
    await _save_draft(client)

    r = await client.post("/checkout/confirm", data={"id": "abc", **CONFIRM_FORM})
    assert r.status == 400                          # non-numeric template id
    r = await client.post("/checkout/confirm",
                          data={"id": str(NEW_ID), "buyer_name": "",
                                "whatsapp": "0", "bank": "BCA"})
    assert r.status == 422                          # missing required name
    r = await client.post("/checkout/confirm",
                          data={"id": str(NEW_ID), **CONFIRM_FORM, "bank": "PAYPAL"})
    assert r.status == 422                          # unknown payment method

    conn = _conn()
    status = conn.execute("SELECT status FROM orders").fetchall()
    conn.close()
    assert status == [("draft",)]                   # nothing mutated


@pytest.mark.asyncio
async def test_confirm_without_draft_forbidden(aiohttp_client):
    client = await aiohttp_client(create_app())
    r = await client.post("/checkout/confirm",
                          data={"id": str(NEW_ID), **CONFIRM_FORM})
    assert r.status == 403
    j = await r.json()
    assert j["ok"] is False


# ---------------------------------------------------------------------------
# M: transaction rollback — failure mid-update leaves no partial state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_rollback_on_failure(aiohttp_client, monkeypatch):
    client = await aiohttp_client(create_app())
    await _save_draft(client)

    import routes.public as pub
    real_log = pub.orders_svc.log_order_event

    def boom(*args, **kwargs):
        raise RuntimeError("simulated event-log failure")

    monkeypatch.setattr(pub.orders_svc, "log_order_event", boom)
    resp = await client.post("/checkout/confirm",
                             data={"id": str(NEW_ID), **CONFIRM_FORM})
    monkeypatch.undo()
    assert resp.status == 500
    j = await resp.json()
    assert j["ok"] is False                         # structured JSON error

    conn = _conn()
    order = conn.execute(
        "SELECT status, payment_unique_code, payment_deadline FROM orders").fetchone()
    ev = conn.execute(
        "SELECT COUNT(*) FROM order_events WHERE to_status='pending_payment'"
    ).fetchone()[0]
    conn.close()
    assert order[0] == "draft"                      # rolled back
    assert order[1] is None and order[2] is None    # no orphan payment fields
    assert ev == 0                                  # no half-written event log


# ---------------------------------------------------------------------------
# N: order status page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_page_owner_and_foreigner(aiohttp_client):
    client = await aiohttp_client(create_app())
    await _save_draft(client)
    r = await client.post("/checkout/confirm",
                          data={"id": str(NEW_ID), **CONFIRM_FORM},
                          allow_redirects=False)
    loc = r.headers["Location"]

    r = await client.get(loc)
    assert r.status == 200
    body = await r.text()
    assert "Pesanan berhasil dibuat." in body
    assert "Menunggu Pembayaran" in body
    assert "belum dikonfirmasi" in body
    assert "SM-" in body

    other = await aiohttp_client(create_app())
    r = await other.get(loc)
    assert r.status == 403                          # not theirs

    r = await client.get("/checkout/status?order=SM-NOPE123")
    assert r.status == 404                          # unknown code


# ---------------------------------------------------------------------------
# O: legacy checkout (no draft) still works
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_checkout_still_works(aiohttp_client):
    client = await aiohttp_client(create_app())
    resp = await client.get(f"/checkout?id={LEGACY_ID}")
    assert resp.status == 200
    body = await resp.text()
    assert "Simulasi Bayar" in body                 # legacy CTA preserved
    assert 'action="/checkout/confirm"' in body     # unified confirm endpoint
    # Legacy guestbook route itself remains functional.
    resp = await client.post("/guestbook", data={"id": str(LEGACY_ID),
                                                 "buyer_name": "Tamu"})
    assert resp.status == 200
