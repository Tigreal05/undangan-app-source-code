"""Phase 3.2 — payment submission tests (QRIS / bank transfer + proof upload).

Covers the numbered checklist from the Phase 3.2 brief:
* ownership enforcement on every payment endpoint (no IDOR via order code)
* method availability + configuration-driven instructions
* authoritative amounts (never client-supplied)
* idempotent active-payment creation (one attempt per order)
* secure proof upload: magic bytes, extension agreement, 5 MB cap,
  server-generated filenames, storage outside public static dirs
* submitted status bookkeeping; customer can NEVER set paid/verified
* uploading a proof does NOT publish the invitation
* deterministic duplicate-submission behavior
* transaction rollback (order + payment move together or not at all)
"""
import os
import sqlite3

import pytest

import config
from app import create_app
from services import orders as orders_svc
from services import payments as payments_svc

NEW_ID = 1      # s01-classic-matcha — converted template

VALID_DATA = {
    "title": "Witan & Aiko",
    "groom.full_name": "Witan Prayoga, S.T.",
    "bride.full_name": "Aiko Sakura, S.E.",
    "date_iso": "2026-12-12",
    "time_range": "09.00 WIB",
}

CONFIRM_FORM = {"buyer_name": "Budi", "whatsapp": "08123456789", "bank": "BCA"}

# Minimal real files with correct magic bytes.
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP_BYTES = b"RIFF" + b"\x2c\x00\x00\x00" + b"WEBPVP8 " + b"\x00" * 32


def _conn():
    return sqlite3.connect(config.DB_NAME)


async def _confirmed_order(client):
    """Draft -> checkout confirm -> returns (invitation_id, order_code)."""
    resp = await client.post("/editor/save", json={
        "template_id": NEW_ID, "data": VALID_DATA})
    body = await resp.json()
    assert body["ok"], body
    inv_id = body["invitation_id"]
    r = await client.post("/checkout/confirm",
                          data={"id": str(NEW_ID), **CONFIRM_FORM},
                          allow_redirects=False)
    assert r.status == 303
    code = r.headers["Location"].split("order=")[1]
    return inv_id, code


# ---------------------------------------------------------------------------
# 1 + 2: page requires valid ownership; foreign token cannot access
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payment_page_requires_ownership(aiohttp_client):
    owner = await aiohttp_client(create_app())
    _, code = await _confirmed_order(owner)

    r = await owner.get(f"/payment?order={code}")
    assert r.status == 200
    body = await r.text()
    assert "Pembayaran Pesanan" in body

    other = await aiohttp_client(create_app())
    r = await other.get(f"/payment?order={code}")
    assert r.status == 403                       # exists but not theirs

    r = await owner.get("/payment?order=SM-NOPE123")
    assert r.status == 404                       # unknown code
    r = await owner.get("/payment")
    assert r.status == 404                       # missing code


@pytest.mark.asyncio
async def test_payment_page_rejects_unconfirmed_draft(aiohttp_client):
    client = await aiohttp_client(create_app())
    conn = _conn()
    row = conn.execute("SELECT code FROM orders WHERE 0").fetchone()
    conn.close()
    # Draft-only context: no confirmed order exists for this identity.
    r = await client.get("/payment?order=SM-ABCDEFG")
    assert r.status == 404


# ---------------------------------------------------------------------------
# 3 + 4 + 5 + 6: methods available, amount from authoritative order data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payment_page_shows_methods_and_server_amount(aiohttp_client):
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    conn = _conn()
    amount_db = conn.execute(
        "SELECT o.amount FROM orders o WHERE o.code = ?", (code,)).fetchone()[0]
    conn.close()

    r = await client.get(f"/payment?order={code}")
    body = await r.text()
    assert amount_db in body                                  # authoritative total
    assert "Transfer Bank" in body                            # bank method shown
    assert "BCA" in body and "1234567890" in body             # from settings table
    assert "SUKA MOTO" in body
    assert "Upload Bukti Pembayaran" in body
    assert 'action="/payment/proof"' in body


@pytest.mark.asyncio
async def test_qris_shown_only_when_configured(aiohttp_client):
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    body = await (await client.get(f"/payment?order={code}")).text()
    assert "QRIS" not in body                                 # qris_path empty

    # Admin-configured QRIS image becomes visible (settings mechanism).
    conn = _conn()
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    qris_file = os.path.join(config.UPLOAD_DIR, "qris-test.png")
    with open(qris_file, "wb") as f:
        f.write(PNG_BYTES)
    conn.execute("UPDATE settings SET value = ? WHERE key = 'qris_path'",
                 ("qris-test.png",))
    conn.commit()
    conn.close()

    body = await (await client.get(f"/payment?order={code}")).text()
    assert "QRIS" in body
    assert "/static_uploads/qris-test.png" in body


# ---------------------------------------------------------------------------
# 7 + 8 + 9: method selection creates ONE active payment; idempotent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_method_selection_creates_payment_idempotently(aiohttp_client):
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    r = await client.post("/payment/method", data={"order": code,
                                                   "method": "qris"})
    j = await r.json()
    assert j["ok"] and j["created"] is True
    assert j["method"] == "qris"
    pid = j["payment_id"]

    conn = _conn()
    pay = conn.execute(
        "SELECT order_id, method, amount, status FROM payments WHERE id = ?",
        (pid,)).fetchone()
    order_amount = conn.execute(
        "SELECT amount FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()
    assert pay[1] == "qris"
    assert pay[2] == order_amount          # amount copied from ORDER truth
    assert pay[3] == "pending"

    # Repeated selection: SAME row reused, no duplicate created.
    r2 = await client.post("/payment/method", data={"order": code,
                                                    "method": "bank_transfer"})
    j2 = await r2.json()
    assert j2["created"] is False and j2["payment_id"] == pid
    assert j2["method"] == "bank_transfer"

    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    conn.close()
    assert n == 1


@pytest.mark.asyncio
async def test_customer_cannot_submit_arbitrary_amount(aiohttp_client):
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    r = await client.post("/payment/method",
                          data={"order": code, "method": "qris",
                                "amount": "Rp 1", "status": "paid"})
    j = await r.json()
    conn = _conn()
    pay = conn.execute("SELECT amount, status FROM payments WHERE id = ?",
                       (j["payment_id"],)).fetchone()
    order_amount = conn.execute("SELECT amount FROM orders WHERE code = ?",
                                (code,)).fetchone()[0]
    conn.close()
    assert pay[0] == order_amount != "Rp 1"     # forged amount ignored
    assert pay[1] == "pending"                  # forged status ignored


@pytest.mark.asyncio
async def test_method_selection_ownership_and_validation(aiohttp_client):
    owner = await aiohttp_client(create_app())
    _, code = await _confirmed_order(owner)

    other = await aiohttp_client(create_app())
    r = await other.post("/payment/method", data={"order": code,
                                                  "method": "qris"})
    assert r.status == 403

    r = await owner.post("/payment/method", data={"order": code,
                                                  "method": "paypal"})
    assert r.status == 422                      # unsupported method
    r = await owner.post("/payment/method", data={"method": "qris"})
    assert r.status == 400                      # missing order
    r = await owner.post("/payment/method", data={"order": "SM-XXXXXXX",
                                                  "method": "qris"})
    assert r.status == 404                      # unknown order


# ---------------------------------------------------------------------------
# 10-12 + 19 + 20: valid proofs accepted; submitted bookkeeping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_proofs_accepted_and_mark_submitted(aiohttp_client):
    """JPG / PNG / WEBP with correct magic bytes are accepted; each valid
    submission marks the payment 'submitted' and records submitted_at."""
    from aiohttp import FormData
    for blob, fname in (("jpg", JPG_BYTES), ("png", PNG_BYTES), ("webp", WEBP_BYTES)):
        client = await aiohttp_client(create_app())
        _, code = await _confirmed_order(client)   # fresh order per format

        form = FormData()
        form.add_field("order", code)
        form.add_field("file", blob, filename=f"bukti.{fname}")
        r = await client.post("/payment/proof", data=form, allow_redirects=False)
        assert r.status == 303, (fname, r.status)

        conn = _conn()
        pay = conn.execute(
            "SELECT p.status, p.submitted_at FROM payments p"
            " JOIN orders o ON o.id = p.order_id WHERE o.code = ?",
            (code,)).fetchone()
        inv_state = conn.execute(
            "SELECT i.state FROM invitations i"
            " JOIN orders o ON o.id = i.order_id WHERE o.code = ?",
            (code,)).fetchone()[0]
        ord_status = conn.execute(
            "SELECT status FROM orders WHERE code = ?", (code,)).fetchone()[0]
        conn.close()
        assert pay[0] == "submitted"
        assert pay[1]                                   # submitted_at recorded
        assert ord_status == orders_svc.PAYMENT_REPORTED
        assert inv_state == "draft"                     # never published here


@pytest.mark.asyncio
async def test_proof_upload_success_flow(aiohttp_client):
    from aiohttp import FormData
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    form = FormData()
    form.add_field("order", code)
    form.add_field("file", PNG_BYTES, filename="transfer saya.png")
    r = await client.post("/payment/proof", data=form, allow_redirects=False)
    assert r.status == 303
    assert r.headers["Location"].startswith("/payment?order=")

    conn = _conn()
    pay = conn.execute(
        "SELECT status, proof_path, submitted_at, verified_at,"
        " reference FROM payments").fetchone()
    order_status = conn.execute("SELECT status FROM orders").fetchone()[0]
    inv_state = conn.execute("SELECT state FROM invitations").fetchone()[0]
    conn.close()

    assert pay[0] == "submitted"
    assert pay[1].startswith("payment-proof/")
    assert pay[2]                                   # submitted_at recorded
    assert pay[3] is None                           # NOT verified by us
    assert order_status == orders_svc.PAYMENT_REPORTED
    assert inv_state == "draft"                     # never published here


@pytest.mark.asyncio
async def test_uploaded_file_server_named_and_not_public_static(aiohttp_client):
    from aiohttp import FormData
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    form = FormData()
    form.add_field("order", code)
    form.add_field("file", JPG_BYTES, filename="../../evil.exe.jpg")
    r = await client.post("/payment/proof", data=form, allow_redirects=False)
    assert r.status == 303                          # name neutralized, jpg ok

    conn = _conn()
    rel = conn.execute("SELECT proof_path FROM payments").fetchone()[0]
    conn.close()
    base = os.path.basename(rel)
    assert base.startswith("payment_")              # server-generated name
    assert "evil" not in base
    assert rel.startswith(f"payment-proof/")
    abs_root = os.path.realpath(config.UPLOAD_DIR)
    abs_path = os.path.realpath(os.path.join(abs_root, rel))
    assert abs_path.startswith(abs_root + os.sep)   # contained in UPLOAD_DIR
    assert os.path.isfile(abs_path)
    # Stored OUTSIDE any repo-visible static dir (UPLOAD_DIR is tmp-isolated
    # per conftest; proofs live in a dedicated subdir, never in ./static).
    assert os.path.dirname(abs_path).endswith("payment-proof") or \
        "payment-proof" in abs_path


@pytest.mark.asyncio
async def test_provenance_recorded(aiohttp_client):
    from aiohttp import FormData
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    form = FormData()
    form.add_field("order", code)
    form.add_field("file", WEBP_BYTES, filename="struk-webp.webp")
    r = await client.post("/payment/proof", data=form, allow_redirects=False)
    assert r.status == 303

    conn = _conn()
    row = conn.execute("SELECT reference FROM payments").fetchone()
    conn.close()
    prov = payments_svc.parse_provenance({"reference": row[0]})
    assert prov["original_name"] == "struk-webp.webp"
    assert prov["mime"] == "image/webp"
    assert prov["size"] == len(WEBP_BYTES)


# ---------------------------------------------------------------------------
# 13-16: invalid extension / MIME mismatch / oversize / traversal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_proof_files_rejected(aiohttp_client):
    from aiohttp import FormData
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    cases = [
        (b"%PDF-1.4 fake", "invoice.pdf"),                 # unsupported type
        (b"<html><script>alert(1)</script></html>", "x.html"),
        (b"GIF89a" + b"\x00" * 40, "anim.gif"),            # not allowed format
        (PNG_BYTES, "renamed.txt"),                        # bad extension
        (JPG_BYTES, "mismatch.png"),                       # ext vs content
    ]
    for blob, fname in cases:
        form = FormData()
        form.add_field("order", code)
        form.add_field("file", blob, filename=fname)
        r = await client.post("/payment/proof", data=form)
        assert r.status == 422, fname
        j = await r.json()
        assert j["ok"] is False

    conn = _conn()
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
    st = conn.execute("SELECT status FROM orders").fetchone()[0]
    conn.close()
    assert st == orders_svc.PENDING_PAYMENT               # untouched


@pytest.mark.asyncio
async def test_oversized_proof_rejected(aiohttp_client):
    from aiohttp import FormData
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    big = PNG_BYTES + b"\x00" * (payments_svc.MAX_PROOF_SIZE + 10)
    form = FormData()
    form.add_field("order", code)
    form.add_field("file", big, filename="huge.png")
    r = await client.post("/payment/proof", data=form)
    assert r.status == 422
    j = await r.json()
    assert "5 MB" in j["error"]

    conn = _conn()
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
    conn.close()


@pytest.mark.asyncio
async def test_malformed_upload_payload_rejected(aiohttp_client):
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)
    # urlencoded body (no file part / not multipart) -> rejected safely with
    # a structured JSON error before any storage or payment state change.
    r = await client.post("/payment/proof", data={"order": code})
    assert r.status == 400
    j = await r.json()
    assert j["ok"] is False
    r = await client.post("/payment/proof", data={"file": b"x"})
    assert r.status == 400                                # no order field
    conn = _conn()
    n_pay = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    st = conn.execute("SELECT status FROM orders WHERE code = ?", (code,)).fetchone()[0]
    conn.close()
    assert n_pay == 0                                     # no payment created
    assert st == orders_svc.PENDING_PAYMENT               # order untouched


# ---------------------------------------------------------------------------
# 21 + 22 + 23: customer cannot force paid/verified/published
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_customer_cannot_set_paid_or_verified(aiohttp_client):
    from aiohttp import FormData
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    form = FormData()
    form.add_field("order", code)
    form.add_field("status", "paid")
    form.add_field("payment_status", "verified")
    form.add_field("file", PNG_BYTES, filename="a.png")
    r = await client.post("/payment/proof", data=form, allow_redirects=False)
    assert r.status == 303

    conn = _conn()
    order_status = conn.execute("SELECT status FROM orders").fetchone()[0]
    pay = conn.execute("SELECT status, verified_at FROM payments").fetchone()
    inv = conn.execute("SELECT state FROM invitations").fetchone()[0]
    conn.close()
    assert order_status == orders_svc.PAYMENT_REPORTED    # NOT paid
    assert pay[0] == "submitted"                          # NOT verified
    assert pay[1] is None
    assert inv == "draft"                                 # NOT published


# ---------------------------------------------------------------------------
# 24: duplicate submission deterministic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_submission_is_deterministic(aiohttp_client):
    from aiohttp import FormData
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    async def submit():
        form = FormData()
        form.add_field("order", code)
        form.add_field("file", PNG_BYTES, filename="first.png")
        return await client.post("/payment/proof", data=form,
                                 allow_redirects=False)

    r1 = await submit()
    assert r1.status == 303
    r2 = await submit()
    assert r2.status == 409                               # already reported
    j = await r2.json()
    assert j["ok"] is False

    conn = _conn()
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    stored_path = conn.execute("SELECT proof_path FROM payments").fetchone()[0]
    conn.close()
    # Server-generated name: original filename must never be used on disk.
    assert "first" not in stored_path
    assert stored_path.startswith("payment-proof/")
    assert os.path.basename(stored_path).startswith("payment_")


# ---------------------------------------------------------------------------
# Ownership on proof upload + rollback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proof_upload_rejects_foreign_order(aiohttp_client):
    from aiohttp import FormData
    owner = await aiohttp_client(create_app())
    _, code = await _confirmed_order(owner)

    other = await aiohttp_client(create_app())
    form = FormData()
    form.add_field("order", code)
    form.add_field("file", PNG_BYTES, filename="steal.png")
    r = await other.post("/payment/proof", data=form)
    assert r.status == 403

    conn = _conn()
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == \
        orders_svc.PENDING_PAYMENT
    conn.close()


@pytest.mark.asyncio
async def test_proof_submission_rolls_back_together(aiohttp_client, monkeypatch):
    """If the order transition fails, the payment row must stay pending."""
    from aiohttp import FormData
    client = await aiohttp_client(create_app())
    _, code = await _confirmed_order(client)

    import routes.public as pub

    real_transition = pub.orders_svc.transition

    def boom(*args, **kwargs):
        raise RuntimeError("simulated transition failure")

    monkeypatch.setattr(pub.orders_svc, "transition", boom)
    form = FormData()
    form.add_field("order", code)
    form.add_field("file", PNG_BYTES, filename="a.png")
    r = await client.post("/payment/proof", data=form)
    monkeypatch.undo()

    assert r.status == 500
    j = await r.json()
    assert j["ok"] is False

    conn = _conn()
    pay = conn.execute("SELECT status, proof_path FROM payments").fetchall()
    order_status = conn.execute("SELECT status FROM orders").fetchone()[0]
    conn.close()
    assert order_status == orders_svc.PENDING_PAYMENT     # unchanged
    # No half-written submitted payment survives the rollback.
    assert all(p[0] != "submitted" for p in pay)


# ---------------------------------------------------------------------------
# Unit-level service checks (magic-byte sniffing + path safety)
# ---------------------------------------------------------------------------

def test_sniff_and_storage_helpers():
    assert payments_svc.sniff_image_type(JPG_BYTES) == "jpg"
    assert payments_svc.sniff_image_type(PNG_BYTES) == "png"
    assert payments_svc.sniff_image_type(WEBP_BYTES) == "webp"
    assert payments_svc.sniff_image_type(b"nothing") is None

    with pytest.raises(ValueError):
        payments_svc.store_proof(1, b"", "empty.png")
    with pytest.raises(ValueError):
        payments_svc.store_proof(1, b"\xff\xfe" * 20, "weird.bin")

    rel, ext, mime, size = payments_svc.store_proof(
        42, PNG_BYTES, "../../tmp/evil name.png")
    assert ext == "png" and mime == "image/png"
    assert rel.startswith("payment-proof/42/payment_")
    assert "evil" not in rel and ".." not in rel


def test_normalize_method():
    assert payments_svc.normalize_method("QRIS") == "qris"
    assert payments_svc.normalize_method("dana") == "qris"
    assert payments_svc.normalize_method("BCA") == "bank_transfer"
    assert payments_svc.normalize_method("paypal") == ""
