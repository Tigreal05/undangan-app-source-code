"""Tier catalogue: single source of truth for features, limits and duration.

The active period lives ONLY here (``active_days`` per tier). Nothing else in
the app may store a duration; pages render it as "30 hari", "90 hari", etc.
"""
from datetime import datetime, timedelta

DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Feature keys (PLAYBOOK section 1 matrix):
# rsvp, wishes, gift, personal_link, music, countdown, map, live_rsvp,
# guest_list, qr, multi_event, custom_slug, custom_colors, custom_fonts,
# custom_cover, custom_background, custom_sections, custom_wording,
# og_preview, custom_design
TIERS = [
    {
        "code": "silver",
        "name": "Silver",
        "subtitle": "Undangan digital siap pakai, tinggal isi data.",
        "positioning": "Undangan digital siap pakai, tinggal isi data.",
        "active_days": 30,
        "price_label": "Rp 150.000",
        "sort_order": 1,
        # Phase 7.5 (B1): 'qr' removed — spec says qr is false for Silver.
        "features": {
            "rsvp", "wishes", "gift", "personal_link", "music",
            "countdown", "map",
        },
        "limits": {
            # Phase 7.5 (B1): aligned with PLAYBOOK section 1 matrix.
            "max_events": 2,
            "max_gallery": 10,
            "max_videos": 1,
            "max_music_mb": 10,
            "max_guests": 100,
            "max_wishes": 200,
            "media_count": 5,
            "custom_requests": 0,
        },
    },
    {
        "code": "gold",
        "name": "Gold",
        "subtitle": "Template premium yang bisa disesuaikan dengan pasangan.",
        "positioning": "Template premium yang bisa disesuaikan dengan pasangan.",
        "active_days": 90,
        "price_label": "Rp 300.000",
        "sort_order": 2,
        "features": {
            "rsvp", "wishes", "gift", "personal_link", "music",
            "countdown", "map", "live_rsvp", "guest_list", "qr",
            "multi_event", "custom_slug", "custom_colors", "custom_fonts",
            "custom_cover", "custom_background", "custom_sections",
            "custom_wording", "og_preview",
        },
        "limits": {
            # Phase 7: multi event up to 6 presets; gallery 30; videos 3.
            "max_events": 6,
            "max_gallery": 30,
            "max_videos": 3,
            "max_music_mb": 10,
            "max_guests": 500,
            "max_wishes": 1000,
            "media_count": 15,
            "custom_requests": 1,
        },
    },
    {
        "code": "platinum",
        "name": "Platinum VIP",
        "subtitle": "Punya request? Kita bikinin.",
        "positioning": "Punya request? Kita bikinin.",
        "active_days": 180,
        "price_label": "Rp 500.000",
        "sort_order": 3,
        "features": {
            "rsvp", "wishes", "gift", "personal_link", "music",
            "countdown", "map", "live_rsvp", "guest_list", "qr",
            "multi_event", "custom_slug", "custom_colors", "custom_fonts",
            "custom_cover", "custom_background", "custom_sections",
            "custom_wording", "og_preview", "custom_design",
        },
        "limits": {
            # Phase 7.5 (B1): Platinum = no limits (very high values).
            "max_events": 99,
            "max_gallery": 999,
            "max_videos": 99,
            "max_music_mb": 50,
            "max_guests": 2000,
            "max_wishes": 5000,
            "media_count": 50,
            "custom_requests": 10,
        },
    },
]

_BY_CODE = {t["code"]: t for t in TIERS}

#: Every limit key known to the system (used by the seeder).
KNOWN_LIMITS = sorted({k for t in TIERS for k in t["limits"]})


def get_tier(tier_code: str) -> dict:
    """Return the tier dict or raise KeyError for unknown codes."""
    return _BY_CODE[str(tier_code)]


def has_feature(tier_code: str, key: str) -> bool:
    """True when the tier includes the given feature key."""
    tier = _BY_CODE.get(str(tier_code))
    if tier is None:
        return False
    return key in tier["features"]


def limit(tier_code: str, key: str) -> int:
    """Numeric limit for the tier; 0 when tier/key is unknown."""
    tier = _BY_CODE.get(str(tier_code))
    if tier is None:
        return 0
    return int(tier["limits"].get(key, 0))


def active_days(tier_code: str) -> int:
    return int(_BY_CODE[str(tier_code)]["active_days"])


def duration_label(tier_code: str) -> str:
    """Human-readable period shown on pages, e.g. '30 hari'."""
    return f"{active_days(tier_code)} hari"


def _parse_dt(value):
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    # Accept ISO ('T' separator) and plain 'YYYY-MM-DD HH:MM:SS'.
    normalized = text.replace("T", " ")
    for fmt in (DATE_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparsable datetime: {value!r}")


def expires_at(tier_code: str, published_at) -> datetime:
    """published_at + tier.active_days — the ONLY place duration is applied."""
    days = active_days(tier_code)
    return _parse_dt(published_at) + timedelta(days=days)
