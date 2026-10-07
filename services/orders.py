"""Order helpers: unique codes and the explicit order state machine.

States (PLAYBOOK section 2.3 + Phase 4 checkout flow):
    draft -> pending_payment -> payment_reported -> paid -> in_protection
          -> published -> expired
    side exits: cancelled (from any non-final state),
    published can also go back to in_protection on renewal hold.

``draft`` is where client-created orders start (Phase 3); moving to checkout
sets ``pending_payment``. While an order sits in ``awaiting_payment`` states a
lazy deadline check cancels it after PAYMENT_DEADLINE_HOURS (24h).
"""
import json
import re
import secrets
import string
from datetime import datetime, timedelta

from services import tiers as tiers_svc

DRAFT = "draft"
NEW = "new"
PENDING_PAYMENT = "pending_payment"
PAYMENT_REPORTED = "payment_reported"
PAID = "paid"
IN_PROTECTION = "in_protection"
PUBLISHED = "published"
EXPIRED = "expired"
CANCELLED = "cancelled"

ALL_STATES = [DRAFT, NEW, PENDING_PAYMENT, PAYMENT_REPORTED, PAID,
              IN_PROTECTION, PUBLISHED, EXPIRED, CANCELLED]

#: States in which an order awaits payment — the payment unique-code pool.
AWAITING_PAYMENT_STATES = (PENDING_PAYMENT,)

#: Hours an order may stay in pending_payment before lazy expiry cancels it.
PAYMENT_DEADLINE_HOURS = 24

#: Explicit allowed-transitions map. Anything not listed here is illegal.
ALLOWED_TRANSITIONS = {
    DRAFT:           [PENDING_PAYMENT, CANCELLED],
    NEW:             [PENDING_PAYMENT, CANCELLED],
    PENDING_PAYMENT: [PAYMENT_REPORTED, CANCELLED],
    PAYMENT_REPORTED: [PAID, PENDING_PAYMENT, CANCELLED],
    PAID:            [IN_PROTECTION, PUBLISHED, CANCELLED],
    IN_PROTECTION:   [PUBLISHED, CANCELLED],
    PUBLISHED:       [EXPIRED, IN_PROTECTION],
    EXPIRED:         [],
    CANCELLED:       [],
}

_TIMESTAMP_FIELDS = {
    PAID: "paid_at",
    PUBLISHED: "published_at",
    EXPIRED: "expired_at",
    CANCELLED: "cancelled_at",
}


class IllegalTransition(Exception):
    """Raised when an order state transition is not allowed."""


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Money / price parsing
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d+")


def parse_price(text) -> int:
    """'Rp 150.000' -> 150000 (integers, rupiah has no cents)."""
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return int(text)
    return int("".join(_NUM_RE.findall(str(text))) or 0)


def format_rupiah(amount) -> str:
    """150000 -> 'Rp 150.000'."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 0
    return "Rp {:,.0f}".format(amount).replace(",", ".")


# ---------------------------------------------------------------------------
# Payment unique code (3 digits, unique among awaiting-payment orders)
# ---------------------------------------------------------------------------

def payment_unique_code(conn=None) -> str:
    """Return a free 3-digit code (001-999) among orders awaiting payment.

    Uniqueness scope: rows whose status is in AWAITING_PAYMENT_STATES and that
    still carry a payment_deadline (i.e. have not been lazily cancelled).
    Returns '' when the pool is exhausted (caller must fail the checkout).
    """
    taken = set()
    if conn is not None:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in AWAITING_PAYMENT_STATES)
        cursor.execute(
            f"SELECT payment_unique_code FROM orders "
            f"WHERE status IN ({placeholders}) AND payment_deadline IS NOT NULL",
            AWAITING_PAYMENT_STATES)
        taken = {row[0] for row in cursor.fetchall() if row[0]}
    candidates = [c for c in ("{:03d}".format(i) for i in range(1, 1000))
                  if c not in taken]
    if not candidates:
        return ""
    return secrets.choice(candidates)


# ---------------------------------------------------------------------------
# Checkout totals
# ---------------------------------------------------------------------------

def compute_total(price_text, discount_text, unique_code: str) -> int:
    """total = price - discount + unique_code (Phase 4 rule).

    The discount label may embed a percentage ('Diskon 10%') and/or an
    absolute amount ('Potongan Rp 25.000'); both are honoured.
    """
    price = parse_price(price_text)
    label = str(discount_text or "")
    discount = 0
    pct_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", label)
    if pct_match:
        discount += price * float(pct_match.group(1).replace(",", ".")) / 100.0
    abs_match = re.search(r"(?:Rp\s*)?([\d.]+)\s*$", label.strip())
    if abs_match and not pct_match:
        discount += parse_price(abs_match.group(1))
    elif abs_match and re.match(r"^\s*Rp", label.strip(), re.IGNORECASE):
        discount += parse_price(abs_match.group(1))
    discount = min(int(round(discount)), price)
    return price - discount + int(unique_code or 0)


# ---------------------------------------------------------------------------
# Lazy payment-deadline enforcement
# ---------------------------------------------------------------------------

def apply_lazy_expiry(conn, order_id=None) -> int:
    """Cancel every order that blew past its 24h payment deadline.

    When ``order_id`` is given, only that order is considered (and the row is
    re-read afterwards so callers see the fresh status).  Returns the number
    of orders cancelled.  Uses the same bookkeeping as transition(CANCELLED)
    so the state machine invariant holds.
    """
    now = _now_str()
    cursor = conn.cursor()
    if order_id is not None:
        cursor.execute(
            "SELECT id FROM orders "
            "WHERE id = ? AND status = ? AND payment_deadline IS NOT NULL"
            " AND payment_deadline < ?",
            (order_id, PENDING_PAYMENT, now))
    else:
        cursor.execute(
            "SELECT id FROM orders "
            "WHERE status = ? AND payment_deadline IS NOT NULL"
            " AND payment_deadline < ?",
            (PENDING_PAYMENT, now))
    ids = [row[0] for row in cursor.fetchall()]
    for oid in ids:
        # pending_payment -> cancelled is always legal per the state machine.
        transition(conn, oid, CANCELLED, actor_note="payment deadline passed")
    return len(ids)


# ---------------------------------------------------------------------------
# Settings table access (admin-editable payment details)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "bank_name": "",
    "bank_number": "",
    "bank_holder": "",
    "qris_path": "",
}


def get_setting(conn, key: str, default: str = "") -> str:
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row and row[0] is not None else default


def get_settings(conn) -> dict:
    values = dict(DEFAULT_SETTINGS)
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    for key, value in cursor.fetchall():
        values[key] = value
    return values


def set_setting(conn, key: str, value: str):
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (key, value, _now_str()))
    conn.commit()


# ---------------------------------------------------------------------------
# Admin notifications
# ---------------------------------------------------------------------------

def notify_admin(conn, kind: str, payload: dict):
    """Insert an admin_notifications row (idempotent helper for all phases)."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO admin_notifications (kind, payload_json) VALUES (?, ?)",
        (kind, json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def unread_notifications(conn, limit: int = 50):
    """Return [(id, kind, payload_dict, created_at)] of unread notifications."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, kind, payload_json, created_at FROM admin_notifications"
        " WHERE read = 0 ORDER BY id DESC LIMIT ?", (int(limit),))
    out = []
    for nid, kind, payload_json, created_at in cursor.fetchall():
        try:
            payload = json.loads(payload_json or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append((nid, kind, payload, created_at))
    return out


def mark_notification_read(conn, notification_id) -> int:
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE admin_notifications SET read = 1 WHERE id = ?",
        (notification_id,))
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Order event log (who / when / from -> to) — Phase 5
# ---------------------------------------------------------------------------

def log_order_event(conn, order_id, *, actor: str, action: str,
                    from_status: str = "", to_status: str = "",
                    note: str = "", commit: bool = True):
    """Append one row to order_events. Never raises on logging problems.

    ``commit=False`` keeps the INSERT in the caller's open transaction
    (Phase 3.2 proof submission rolls payment + order + event together).
    """
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO order_events"
            " (order_id, actor, action, from_status, to_status, note)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, actor, action, from_status, to_status, note))
        if commit:
            conn.commit()
    except Exception:
        # Logging must never break a business transition; the DB may be old.
        pass


# ---------------------------------------------------------------------------
# Public slug generation (Phase 5 publish step)
# ---------------------------------------------------------------------------

_TRANSLIT = str.maketrans({
    "à": "a", "á": "a", "â": "a", "ã": "a", "ä": "a", "å": "a",
    "è": "e", "é": "e", "ê": "e", "ë": "e",
    "ì": "i", "í": "i", "î": "i", "ï": "i",
    "ò": "o", "ó": "o", "ô": "o", "õ": "o", "ö": "o",
    "ù": "u", "ú": "u", "û": "u", "ü": "u",
    "ñ": "n", "ç": "c", "&": " and ",
})


def slugify(text: str) -> str:
    """'Rian & Siska' -> 'rian-dan-siska'; accents are transliterated, any
    other non-alphanumeric run collapses to single hyphens."""
    s = str(text or "").strip().lower()
    s = s.replace("&", " dan ")
    s = s.translate(_TRANSLIT)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def generate_slug(conn, names_text: str, *, allow_custom: bool = False,
                  custom: str = "") -> str:
    """Build a unique invitation slug from the couple names.

    * base = slugify(names); falls back to 'undangan'.
    * Silver (allow_custom=False) always derives the slug from the names —
      a supplied ``custom`` value is ignored.
    * Gold/Platinum may supply ``custom`` (still slugified).
    * On collision with an existing invitations.slug the suffix -2, -3 ...
      is appended until free.
    """
    base = ""
    if allow_custom and (custom or "").strip():
        base = slugify(custom)
    if not base:
        base = slugify(names_text)
    if not base:
        base = "undangan"
    cursor = conn.cursor()
    slug = base
    n = 1
    while True:
        cursor.execute("SELECT 1 FROM invitations WHERE slug = ? LIMIT 1", (slug,))
        if cursor.fetchone() is None:
            return slug
        n += 1
        slug = f"{base}-{n}"


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, [])


def new_order_code(conn=None) -> str:
    """Generate a unique human-friendly order code, e.g. 'SM-7K3QD9'."""
    while True:
        code = "SM-" + "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)
        )
        if conn is None:
            return code
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM orders WHERE code = ? LIMIT 1", (code,))
        if cursor.fetchone() is None:
            return code


def unique_code(length: int = 8, conn=None, table: str = "invitations",
                column: str = "slug") -> str:
    """Random lowercase alphanumeric code; checked against the DB when given."""
    alphabet = string.ascii_lowercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        if conn is None:
            return code
        cursor = conn.cursor()
        cursor.execute(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (code,))
        if cursor.fetchone() is None:
            return code


def transition(conn, order_id, target: str, *, actor_note: str = "",
               actor: str = "system", commit: bool = True) -> str:
    """Move an order to ``target`` enforcing the state machine.

    Returns the new status. Raises IllegalTransition for unknown states or
    disallowed moves. Sets the matching timestamp column and refreshes
    ``expires_at`` from the tier when the invitation gets published.
    Every successful move is logged in ``order_events`` (who/when/from->to).

    ``commit=False`` keeps the UPDATE inside the caller's transaction so a
    multi-table move (e.g. Phase 3.2 payment + order) can roll back together.
    """
    current_status = ALLOWED_TRANSITIONS.keys()
    if target not in current_status:
        raise IllegalTransition(f"unknown target status: {target!r}")

    cursor = conn.cursor()
    cursor.execute("SELECT status, tier_id FROM orders WHERE id = ?", (order_id,))
    row = cursor.fetchone()
    if row is None:
        raise IllegalTransition(f"order {order_id!r} does not exist")
    current, tier_id = row

    if target not in ALLOWED_TRANSITIONS.get(current, []):
        raise IllegalTransition(f"{current} -> {target} is not allowed")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sets = ["status = ?", "updated_at = ?"]
    params = [target, now]

    ts_field = _TIMESTAMP_FIELDS.get(target)
    if ts_field:
        sets.append(f"{ts_field} = ?")
        params.append(now)

    if current == PENDING_PAYMENT and target != PENDING_PAYMENT:
        # Leaving the awaiting-payment pool frees the 3-digit unique code.
        sets.append("payment_unique_code = NULL")
        sets.append("payment_deadline = NULL")

    if target == PUBLISHED:
        # Duration comes ONLY from the tier: expires_at = published + active_days.
        try:
            tier_code = _tier_code_for_id(conn, tier_id)
            exp = tiers_svc.expires_at(tier_code, now)
            sets.append("expires_at = ?")
            params.append(exp.strftime("%Y-%m-%d %H:%M:%S"))
        except KeyError:
            pass

    params.append(order_id)
    cursor.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", params)
    if commit:
        conn.commit()

    # Phase 5: every status change is logged (who, when, from -> to).
    log_order_event(conn, order_id, actor=actor, action=f"transition:{target}",
                    from_status=current, to_status=target, note=actor_note,
                    commit=commit)
    return target


def _tier_code_for_id(conn, tier_id):
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM tiers WHERE id = ?", (tier_id,))
    row = cursor.fetchone()
    if row is None:
        raise KeyError(tier_id)
    return row[0]
