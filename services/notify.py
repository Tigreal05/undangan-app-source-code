"""Admin notification helpers (Phase 5).

* ``wa_link`` builds a wa.me deep-link with a prefilled Indonesian report —
  used both for the ADMIN inbox links and the "Kirim link ke klien" button.
* ``send_telegram`` posts a message through the Bot API when
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are configured; silent no-op
  otherwise (and never raises — notifications must not break requests).
"""
import re
from urllib.parse import quote

import aiohttp

import config

_WA_BASE = "https://wa.me/"


def normalize_wa(number: str) -> str:
    """'0812-3456 789' -> '628123456789' (digits only, leading 0 -> 62)."""
    digits = re.sub(r"\D", "", str(number or ""))
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    return digits


def wa_link(number: str, text: str) -> str:
    """Return a wa.me URL with a percent-encoded prefilled message."""
    return f"{_WA_BASE}{normalize_wa(number)}?text={quote(text, safe='')}"


def order_report(order: dict, admin_page_url: str) -> str:
    """Prefilled WA report for a new order / payment (Indonesian)."""
    lines = [
        "Pesanan Baru SUKA MOTO",
        f"Kode: {order.get('code', '')}",
        f"Nama: {order.get('buyer_name', '')}",
        f"Tier: {order.get('tier_name') or order.get('tier_code', '')}",
        f"Template: {order.get('template_name', '-')}",
        f"Total: {order.get('total') or order.get('amount', '')}",
        f"Status: {order.get('status', '')}",
        f"Detail: {admin_page_url}",
    ]
    return "\n".join(lines)


def client_publish_message(invitation_url: str, couple_names: str = "") -> str:
    """Prefilled WA message sent to the CLIENT with their live link."""
    who = f" undangan untuk {couple_names}" if couple_names else " undangan Anda"
    return (
        f"Halo, pesanan{who} sudah aktif ya. "
        f"Berikut link undangan digitalnya:\n{invitation_url}\n"
        "Terima kasih sudah menggunakan SUKA MOTO!"
    )


def telegram_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


async def send_telegram(text: str) -> bool:
    """Send ``text`` via the Telegram Bot API. Returns True on success.

    Silent no-op when the bot is not configured; swallows all network errors
    so a broken Telegram integration can never fail an admin action.
    """
    if not telegram_configured():
        return False
    url = (f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
           f"/sendMessage")
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                return resp.status == 200
    except Exception:
        return False
