"""Order helpers: unique codes and the explicit order state machine.

States (PLAYBOOK section 2.3):
    new -> pending_payment -> paid -> in_protection -> published -> expired
    side exits: cancelled (from any non-final state),
    published can also go back to in_protection on renewal hold.
"""
import secrets
import string
from datetime import datetime

from services import tiers as tiers_svc

NEW = "new"
PENDING_PAYMENT = "pending_payment"
PAID = "paid"
IN_PROTECTION = "in_protection"
PUBLISHED = "published"
EXPIRED = "expired"
CANCELLED = "cancelled"

ALL_STATES = [NEW, PENDING_PAYMENT, PAID, IN_PROTECTION, PUBLISHED, EXPIRED, CANCELLED]

#: Explicit allowed-transitions map. Anything not listed here is illegal.
ALLOWED_TRANSITIONS = {
    NEW:             [PENDING_PAYMENT, CANCELLED],
    PENDING_PAYMENT: [PAID, CANCELLED],
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


def transition(conn, order_id, target: str, *, actor_note: str = "") -> str:
    """Move an order to ``target`` enforcing the state machine.

    Returns the new status. Raises IllegalTransition for unknown states or
    disallowed moves. Sets the matching timestamp column and refreshes
    ``expires_at`` from the tier when the invitation gets published.
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
    conn.commit()
    return target


def _tier_code_for_id(conn, tier_id):
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM tiers WHERE id = ?", (tier_id,))
    row = cursor.fetchone()
    if row is None:
        raise KeyError(tier_id)
    return row[0]
