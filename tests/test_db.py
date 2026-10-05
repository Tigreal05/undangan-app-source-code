"""Phase 1 tests: migrations, tier catalogue, order state machine, expires_at."""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import db as db_mod
from services import orders as orders_svc
from services import tiers as tiers_svc


def fresh_conn(tmp_path):
    return sqlite3.connect(str(tmp_path / "migrate.db"))


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def test_migration_runs_twice_without_error(tmp_path):
    conn = fresh_conn(tmp_path)
    try:
        v1 = db_mod.migrate(conn)
        db_mod._seed_data(conn.cursor())   # migrate only creates tables; seed fills data
        conn.commit()
        rows_before = conn.execute("SELECT COUNT(*) FROM tiers").fetchone()[0]
        v2 = db_mod.migrate(conn)          # second run must be a no-op
        db_mod._seed_data(conn.cursor())   # seeding twice must not duplicate rows
        conn.commit()
        rows_after = conn.execute("SELECT COUNT(*) FROM tiers").fetchone()[0]
        assert v1 == v2
        assert rows_before == rows_after == 3
        assert conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 4
    finally:
        conn.close()


def test_schema_version_table_tracks_version(tmp_path):
    conn = fresh_conn(tmp_path)
    try:
        version = db_mod.migrate(conn)
        stored = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert stored == version == max(db_mod._MIGRATIONS)
    finally:
        conn.close()


def test_backup_created_before_migrating(tmp_path, monkeypatch):
    # Pre-existing non-empty DB file must be copied to backups/ before migrating.
    db_file = tmp_path / "existing.db"
    seed = sqlite3.connect(str(db_file))
    seed.execute("CREATE TABLE legacy (x TEXT)")
    seed.execute("INSERT INTO legacy VALUES ('old data')")
    seed.commit()
    seed.close()
    monkeypatch.setattr(config, "DB_NAME", str(db_file))
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(db_mod, "_BACKUP_CREATED", False)

    conn = sqlite3.connect(str(db_file))
    try:
        db_mod.migrate(conn)
    finally:
        conn.close()
    backups = os.listdir(tmp_path / "backups")
    assert len(backups) == 1
    assert backups[0].startswith("existing.db.")


EXPECTED_TABLES = [
    "tiers", "clients", "orders", "invitations", "invitation_events",
    "media", "guests", "rsvps", "wishes", "payments", "custom_requests",
    "admin_notifications", "packages", "templates", "media_uploads",
    "schema_version",
]


def test_all_spec_tables_exist(tmp_path):
    conn = fresh_conn(tmp_path)
    try:
        db_mod.migrate(conn)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in EXPECTED_TABLES:
            assert table in names, f"missing table {table}"
        tmpl_cols = {r[1] for r in conn.execute("PRAGMA table_info(templates)")}
        for col in ("code", "tier_id", "source_dir", "schema_json",
                    "preview_json", "active"):
            assert col in tmpl_cols, f"missing templates.{col}"
        # Legacy packages table kept and mapped: seeded templates carry tier_id.
        db_mod._seed_data(conn.cursor())
        conn.commit()
        row = conn.execute(
            "SELECT t.package_id, t.tier_id, ti.code FROM templates t"
            " JOIN tiers ti ON ti.id = t.tier_id LIMIT 1").fetchone()
        assert row is not None and row[0] is not None  # package_id -> tier_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tiers: seed + catalogue helpers
# ---------------------------------------------------------------------------

def _seeded_conn(tmp_path):
    conn = fresh_conn(tmp_path)
    db_mod.migrate(conn)
    db_mod._seed_data(conn.cursor())
    conn.commit()
    return conn


def test_tiers_seeded_with_positioning_and_days(tmp_path):
    conn = _seeded_conn(tmp_path)
    try:
        rows = dict((r[0], r) for r in conn.execute(
            "SELECT code, name, subtitle, positioning, active_days,"
            " limits_json, features_json FROM tiers"))
        assert set(rows) == {"silver", "gold", "platinum"}
        assert rows["silver"][4] == 30
        assert rows["gold"][4] == 90
        assert rows["platinum"][4] == 180
        assert rows["silver"][2] == "Undangan digital siap pakai, tinggal isi data."
        assert rows["gold"][2] == "Template premium yang bisa disesuaikan dengan pasangan."
        assert rows["platinum"][1] == "Platinum VIP"
        assert rows["platinum"][2] == "Punya request? Kita bikinin."
        # limits/features are valid JSON mirrors of the catalogue.
        limits = json.loads(rows["gold"][5])
        feats = json.loads(rows["gold"][6])
        assert limits["max_guests"] == tiers_svc.limit("gold", "max_guests")
        assert "custom_design" not in feats and "live_rsvp" in feats
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 7.5 (B2): authoritative spec matrix (PLAYBOOK section 1).
# ---------------------------------------------------------------------------

#: Features that are TRUE for Silver (and therefore for every higher tier).
SILVER_FEATURES = {"rsvp", "wishes", "gift", "personal_link", "music",
                   "countdown", "map"}
#: Features added by Gold on top of Silver's set.
GOLD_EXTRA_FEATURES = {"live_rsvp", "guest_list", "qr", "multi_event",
                       "custom_slug", "custom_colors", "custom_fonts",
                       "custom_cover", "custom_background", "custom_sections",
                       "custom_wording", "og_preview"}
#: The only feature left for Platinum.
PLATINUM_EXTRA_FEATURES = {"custom_design"}
ALL_FEATURES = SILVER_FEATURES | GOLD_EXTRA_FEATURES | PLATINUM_EXTRA_FEATURES

SPEC_FEATURES = {
    "silver": SILVER_FEATURES,
    "gold": SILVER_FEATURES | GOLD_EXTRA_FEATURES,
    "platinum": ALL_FEATURES,
}

#: Spec-mandated limits (gallery / video / events per PLAYBOOK section 1);
#: Platinum is "no limits" => very high values.
SPEC_LIMITS = {
    "silver": {"max_gallery": 10, "max_videos": 1, "max_events": 2},
    "gold": {"max_gallery": 30, "max_videos": 3, "max_events": 6},
    "platinum": {"max_gallery": 999, "max_videos": 99, "max_events": 99},
}


def test_has_feature_matrix():
    """Full spec matrix for all three tiers (Phase 7.5 B2)."""
    for code, expected in SPEC_FEATURES.items():
        for key in ALL_FEATURES:
            got = tiers_svc.has_feature(code, key)
            assert got == (key in expected), (code, key, got)
    # Explicit spot-checks from the spec text.
    assert tiers_svc.has_feature("gold", "custom_cover")        # gold HAS it
    assert not tiers_svc.has_feature("silver", "qr")            # qr is NOT silver
    assert not tiers_svc.has_feature("gold", "custom_design")   # bespoke-only
    assert tiers_svc.has_feature("platinum", "custom_design")
    # Unknown tier/key never explodes.
    assert tiers_svc.has_feature("bronze", "rsvp") is False
    assert tiers_svc.has_feature("gold", "nope") is False


def test_limit_values():
    """Spec limits for gallery/video/events + extras, all three tiers."""
    for code, expected in SPEC_LIMITS.items():
        for key, value in expected.items():
            got = tiers_svc.limit(code, key)
            assert got == value, (code, key, got, value)
    # Non-spec limits still monotonic silver < gold < platinum.
    assert (tiers_svc.limit("silver", "max_guests")
            < tiers_svc.limit("gold", "max_guests")
            < tiers_svc.limit("platinum", "max_guests"))
    assert tiers_svc.limit("silver", "custom_requests") == 0
    assert tiers_svc.limit("platinum", "custom_requests") == 10
    assert tiers_svc.limit("silver", "unknown_key") == 0
    assert tiers_svc.limit("nope", "max_guests") == 0


def test_seeded_db_matches_spec_matrix(tmp_path):
    """The DB seed/migration must mirror the spec, not just the Python dict."""
    conn = _seeded_conn(tmp_path)
    try:
        for code, expected in SPEC_FEATURES.items():
            row = conn.execute(
                "SELECT features_json, limits_json FROM tiers WHERE code = ?",
                (code,)).fetchone()
            feats = set(json.loads(row[0]))
            lims = json.loads(row[1])
            assert feats == expected, code
            for key, value in SPEC_LIMITS[code].items():
                assert lims[key] == value, (code, key, lims[key], value)
    finally:
        conn.close()


def test_migration_9_repairs_stale_silver_row(tmp_path):
    """Migration 9 fixes an already-seeded DB with the buggy Silver row."""
    conn = fresh_conn(tmp_path)
    try:
        db_mod.migrate(conn)
        db_mod._seed_data(conn.cursor())
        conn.commit()
        # Simulate the pre-fix production state directly in the DB.
        conn.execute(
            "UPDATE tiers SET features_json = ?, limits_json = ?"
            " WHERE code = 'silver'",
            (json.dumps(sorted(SILVER_FEATURES | {"qr"})),
             json.dumps({"max_events": 1, "max_gallery": 5, "max_videos": 0})))
        conn.execute("UPDATE schema_version SET version = 8")
        conn.commit()
        db_mod.migrate(conn)   # applies migration 9
        conn.commit()
        row = conn.execute(
            "SELECT features_json, limits_json FROM tiers WHERE code='silver'"
        ).fetchone()
        feats = set(json.loads(row[0]))
        lims = json.loads(row[1])
        assert "qr" not in feats and feats == SPEC_FEATURES["silver"]
        assert lims["max_events"] == 2 and lims["max_gallery"] == 10 \
            and lims["max_videos"] == 1
    finally:
        conn.close()


def test_duration_label_from_tier_only():
    assert tiers_svc.duration_label("silver") == "30 hari"
    assert tiers_svc.duration_label("gold") == "90 hari"
    assert tiers_svc.duration_label("platinum") == "180 hari"


# ---------------------------------------------------------------------------
# Order codes & state machine
# ---------------------------------------------------------------------------

def test_new_order_code_unique(tmp_path):
    conn = _seeded_conn(tmp_path)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO clients (name) VALUES ('X')")
        client_id = cur.lastrowid
        tier_id = conn.execute("SELECT id FROM tiers WHERE code='gold'").fetchone()[0]
        codes = set()
        for _ in range(5):
            code = orders_svc.new_order_code(conn)
            assert code.startswith("SM-") and len(code) == 9
            assert code not in codes
            codes.add(code)
            cur.execute(
                "INSERT INTO orders (code, client_id, tier_id) VALUES (?, ?, ?)",
                (code, client_id, tier_id))
        conn.commit()
        # unique_code checks against the given table/column too.
        slug = orders_svc.unique_code(conn=conn, table="orders", column="code")
        assert slug not in codes
    finally:
        conn.close()


def test_legal_transition_chain(tmp_path):
    conn = _seeded_conn(tmp_path)
    try:
        cur = conn.cursor()
        tier_id = conn.execute("SELECT id FROM tiers WHERE code='silver'").fetchone()[0]
        cur.execute("INSERT INTO orders (code, tier_id, status) VALUES ('SM-TST001', ?, 'new')",
                    (tier_id,))
        oid = cur.lastrowid
        conn.commit()

        assert orders_svc.transition(conn, oid, "pending_payment") == "pending_payment"
        assert orders_svc.transition(conn, oid, "payment_reported") == "payment_reported"
        assert orders_svc.transition(conn, oid, "paid") == "paid"
        assert orders_svc.transition(conn, oid, "in_protection") == "in_protection"
        assert orders_svc.transition(conn, oid, "published") == "published"
        assert orders_svc.transition(conn, oid, "expired") == "expired"

        row = conn.execute(
            "SELECT status, paid_at, published_at, expired_at, expires_at"
            " FROM orders WHERE id = ?", (oid,)).fetchone()
        assert row[0] == "expired"
        assert row[1] and row[2] and row[3]
        # expires_at == published_at + 30 days (silver tier).
        published = datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S")
        expiry = datetime.strptime(row[4], "%Y-%m-%d %H:%M:%S")
        assert expiry == published + timedelta(days=30)
    finally:
        conn.close()


ILLEGAL_MOVES = [
    ("new", "paid"),
    ("new", "published"),
    ("new", "expired"),
    ("draft", "paid"),
    ("draft", "published"),
    ("pending_payment", "published"),
    ("pending_payment", "paid"),  # must go through payment_reported first
    ("payment_reported", "published"),
    ("payment_reported", "in_protection"),
    ("paid", "new"),
    ("in_protection", "paid"),
    ("published", "paid"),
    ("expired", "published"),
    ("expired", "new"),
    ("cancelled", "paid"),
]


@pytest.mark.parametrize("current,target", ILLEGAL_MOVES)
def test_illegal_transitions_raise(tmp_path, current, target):
    conn = _seeded_conn(tmp_path)
    try:
        tier_id = conn.execute("SELECT id FROM tiers WHERE code='gold'").fetchone()[0]
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (code, tier_id, status) VALUES ('SM-ILL001', ?, ?)",
            (tier_id, current))
        oid = cur.lastrowid
        conn.commit()
        with pytest.raises(orders_svc.IllegalTransition):
            orders_svc.transition(conn, oid, target)
        still = conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0]
        assert still == current
    finally:
        conn.close()


def test_unknown_target_raises():
    with pytest.raises(orders_svc.IllegalTransition):
        orders_svc.transition(sqlite3.connect(":memory:"), 1, "shipped")


# ---------------------------------------------------------------------------
# expires_at(tier_code, published_at)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,days", [("silver", 30), ("gold", 90), ("platinum", 180)])
def test_expires_at_adds_active_days(code, days):
    base = datetime(2026, 10, 5, 12, 30, 0)
    assert tiers_svc.expires_at(code, base) == base + timedelta(days=days)
    # Also accepts DB-style strings.
    assert tiers_svc.expires_at(code, "2026-10-05 12:30:00") == base + timedelta(days=days)


@pytest.mark.asyncio
async def test_pages_render_tier_period_not_legacy_duration(aiohttp_client):
    """/checkout shows '90 hari' (tier) even though templates.duration says otherwise."""
    from app import create_app
    from db import get_conn

    client = await aiohttp_client(create_app())  # boots + seeds
    conn = get_conn()
    gold_id = conn.execute("SELECT id FROM tiers WHERE code='gold'").fetchone()[0]
    pkg_id = conn.execute("SELECT id FROM packages WHERE name='Gold Package'").fetchone()[0]
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO templates (package_id, name, price, discount, duration,"
        " image_url, html_code, is_top10, tier_id)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (pkg_id, "Tier Test", "Rp 1", "", "Aktif Selamanya", "/x.png",
         "<html><body>x</body></html>", 0, gold_id))
    tid = cur.lastrowid
    conn.commit()
    conn.close()

    resp = await client.get(f"/checkout?id={tid}")
    body = await resp.text()
    assert "90 hari" in body
    assert "Aktif Selamanya" not in body

    resp = await client.get(f"/template-action?id={tid}")
    assert "90 hari" in await resp.text()

    resp = await client.get("/templates?package_id=%d" % pkg_id)
    assert "90 hari" in await resp.text()

    conn = get_conn()
    conn.execute("DELETE FROM templates WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
