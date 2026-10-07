"""Payment submission helpers (Phase 3.2).

Responsibilities:
* locate / create the ACTIVE payment record for an order (idempotent —
  at most one active attempt per order),
* authoritative amount formatting (always derived from the order row,
  never from client input),
* server-side payment configuration access (settings table via
  services/orders.py — no hardcoded bank/QRIS data in routes/templates),
* secure proof storage under ``UPLOAD_DIR/payment-proof/<order-id>/`` with
  content-signature validation and server-generated filenames.

State ownership (deliberate boundary):
* customer flow may only produce ``pending`` / ``submitted`` payment rows;
  ``verified`` / ``rejected`` belong to Phase 3.3 admin verification.
* uploading a proof moves the ORDER via the existing state machine
  pending_payment -> payment_reported; it NEVER marks the order paid and
  NEVER publishes anything.
"""
import json
import os
import re
import secrets

from db import get_conn
from services import orders as orders_svc

#: Payment statuses the customer flow is allowed to create/keep active.
ACTIVE_STATUSES = ("pending", "submitted")
SUBMITTED = "submitted"

#: Proof upload policy (Phase 3.2 spec): images only, max 5 MB.
PROOF_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_PROOF_SIZE = 5 * 1024 * 1024

_METHOD_RE = re.compile(r"^[a-z_]{3,20}$")


def normalize_method(raw) -> str:
    """Map any client-supplied method label to 'qris' | 'bank_transfer' | ''."""
    s = str(raw or "").strip().lower()
    if s in ("qris", "dana", "qr"):
        return "qris"
    if s in ("bank_transfer", "transfer", "bca", "bank"):
        return "bank_transfer"
    return ""


# ---------------------------------------------------------------------------
# Authoritative amounts — ONLY from the order row, never from the client
# ---------------------------------------------------------------------------

def order_total(conn, order_id) -> int:
    """Return the order's payable total in rupiah (int).

    Prefers the stored order.amount (set by checkout); falls back to
    recomputing from DB template/tier prices + the stored unique code.
    Raises LookupError when the order does not exist.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT o.amount, o.payment_unique_code, t.price, t.discount"
        " FROM orders o LEFT JOIN templates t ON t.id = o.template_id"
        " WHERE o.id = ?", (order_id,))
    row = cursor.fetchone()
    if row is None:
        raise LookupError(f"order {order_id!r} does not exist")
    amount, puc, price, discount = row
    parsed = orders_svc.parse_price(amount)
    if parsed > 0:
        return parsed
    return orders_svc.compute_total(price, discount, puc or "")


def format_amount(conn, order_id) -> str:
    return orders_svc.format_rupiah(order_total(conn, order_id))


# ---------------------------------------------------------------------------
# Active payment record (idempotent)
# ---------------------------------------------------------------------------

def get_active_payment(cursor, order_id):
    """Return the current active payment row dict, or None.

    'Active' = status in ('pending', 'submitted'). A rejected/verified
    payment is historical; starting a new attempt after rejection creates
    a fresh row (deterministic: newest active row wins).
    """
    cursor.execute(
        "SELECT id, order_id, method, amount, reference, proof_path,"
        " status, submitted_at, created_at, updated_at FROM payments"
        " WHERE order_id = ? AND status IN (?, ?)"
        " ORDER BY id DESC LIMIT 1", (order_id,) + ACTIVE_STATUSES)
    row = cursor.fetchone()
    if row is None:
        return None
    keys = ("id", "order_id", "method", "amount", "reference", "proof_path",
            "status", "submitted_at", "created_at", "updated_at")
    return dict(zip(keys, row))


def ensure_payment(conn, order_id, method: str):
    """Create-or-reuse the active payment attempt for an order.

    Idempotent: repeated calls with the same/different method reuse the
    existing active row (updating only its method) instead of creating
    unlimited duplicates. Amount is ALWAYS re-read from the order row.
    Returns (payment_dict, created_bool).
    """
    if not _METHOD_RE.match(method or ""):
        raise ValueError("invalid payment method")
    now = orders_svc._now_str()
    cursor = conn.cursor()
    existing = get_active_payment(cursor, order_id)
    if existing is not None:
        if existing["method"] != method:
            cursor.execute(
                "UPDATE payments SET method = ?, amount = ?, updated_at = ?"
                " WHERE id = ?",
                (method, format_amount(conn, order_id), now, existing["id"]))
            conn.commit()
            existing["method"] = method
        return existing, False
    amount = format_amount(conn, order_id)
    reference = "PAY-" + secrets.token_hex(6).upper()
    cursor.execute(
        "INSERT INTO payments (order_id, method, amount, reference, proof_path,"
        " status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, '', 'pending', ?, ?)",
        (order_id, method, amount, reference, now, now))
    conn.commit()
    pid = cursor.lastrowid
    return {
        "id": pid, "order_id": order_id, "method": method, "amount": amount,
        "reference": reference, "proof_path": "", "status": "pending",
        "submitted_at": None, "created_at": now, "updated_at": now,
    }, True


# ---------------------------------------------------------------------------
# Content-signature validation (never trust the browser MIME alone)
# ---------------------------------------------------------------------------

def sniff_image_type(data: bytes):
    """Return the real image type from magic bytes, or None when unknown."""
    if len(data) < 12:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ---------------------------------------------------------------------------
# Secure proof storage — inside UPLOAD_DIR, NOT a public/static directory
# ---------------------------------------------------------------------------

def proof_dir(order_id) -> str:
    """Absolute storage dir for an order's proofs (traversal-safe)."""
    root = os.path.realpath(config_upload_dir())
    sub = f"payment-proof/{int(order_id)}"          # int() neutralizes ids
    target = os.path.realpath(os.path.join(root, sub))
    if not (target == root or target.startswith(root + os.sep)):
        raise ValueError("proof path escapes upload root")           # defensive
    return target


def config_upload_dir() -> str:
    import config
    return config.UPLOAD_DIR


def store_proof(order_id, data: bytes, original_name: str = ""):
    """Validate + persist one proof file. Returns (rel_path, ext, mime, size).

    rel_path is relative to config.UPLOAD_DIR (same convention as the admin
    media uploader / safe_upload_path readers). Never uses the original
    filename as a filesystem path — the stored name is server-generated.
    Raises ValueError on any policy violation.
    """
    if not data:
        raise ValueError("File bukti pembayaran kosong.")
    if len(data) > MAX_PROOF_SIZE:
        raise ValueError("Ukuran file melebihi batas 5 MB.")
    real_type = sniff_image_type(data)
    if real_type is None:
        raise ValueError("Isi file bukan gambar yang didukung (JPG/PNG/WEBP).")
    # Extension (if the client supplied one) must agree with the real type.
    base = os.path.basename(str(original_name or "").replace("\\", "/"))
    if base and "." in base:
        claimed = base.rsplit(".", 1)[-1].lower()
        if claimed not in PROOF_EXTENSIONS:
            raise ValueError("Ekstensi file tidak diizinkan.")
        if claimed == "jpeg":
            claimed = "jpg"
        if claimed != real_type:
            raise ValueError("Tipe file tidak sesuai dengan isi file.")
    ext = real_type
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext]
    dirname = proof_dir(order_id)
    os.makedirs(dirname, exist_ok=True)
    filename = f"payment_{secrets.token_hex(12)}.{ext}"
    abs_path = os.path.join(dirname, filename)
    with open(abs_path, "wb") as f:
        f.write(data)
    rel_path = f"payment-proof/{int(order_id)}/{filename}"
    return rel_path, ext, mime, len(data)


# ---------------------------------------------------------------------------
# Submission bookkeeping
# ---------------------------------------------------------------------------

def mark_submitted(conn, payment_id, *, rel_path: str, original_name: str,
                   mime: str, size: int):
    """Flip ONE still-pending payment row to 'submitted' atomically.

    The guarded UPDATE (status='pending') makes duplicate submissions
    deterministic: exactly one concurrent submitter wins; others see the
    already-submitted state. Does NOT touch the order status here — the
    route performs the state-machine transition inside the SAME transaction
    so payment + order move together (rollback on any failure).
    Returns rowcount (0 = lost race / not pending anymore). Caller commits.
    """
    now = orders_svc._now_str()
    meta = json.dumps({
        "original_name": os.path.basename(str(original_name or ""))[:120],
        "mime": mime, "size": int(size),
    }, ensure_ascii=False)[:450]
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE payments SET status = ?, proof_path = ?, reference = ?,"
        " submitted_at = ?, verified_at = NULL, updated_at = ?"
        " WHERE id = ? AND status = 'pending'",
        (SUBMITTED, rel_path, f"PROOF:{meta}", now, now, payment_id))
    return cursor.rowcount


def parse_provenance(payment_row) -> dict:
    """Decode the stored provenance JSON from payments.reference.

    Returns {'original_name', 'mime', 'size'} for a submitted proof row,
    else {}. Provenance is packed into the existing ``reference`` column as
    ``PROOF:<json>`` so Phase 3.2 needs no schema change at all.
    """
    ref = str((payment_row or {}).get("reference") or "")
    if ref.startswith("PROOF:"):
        try:
            data = json.loads(ref[len("PROOF:"):])
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    return {}
