"""SQLite database helpers: connection factory, versioned migrations and seeding.

Migration system
----------------
* ``schema_version`` table stores the highest applied migration number.
* Every migration is a numbered function registered through ``@migration(n)``.
* Migrations must be *additive* (CREATE TABLE IF NOT EXISTS / ALTER TABLE ADD
  COLUMN guarded by PRAGMA table_info) and *idempotent* (running them twice on
  the same database never raises).
* Before the first migration of a run the database file is copied into
  ``backups/`` (config.BACKUP_DIR), timestamped.
"""
import glob
import os
import shutil
import sqlite3
import time

import config

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_conn():
    """Return a new SQLite connection (same behavior as the original app)."""
    return sqlite3.connect(config.DB_NAME)


# ---------------------------------------------------------------------------
# Migration framework
# ---------------------------------------------------------------------------

_MIGRATIONS = {}          # number -> callable(cursor)
_BACKUP_CREATED = False   # at most one backup per process/run


def migration(number):
    """Register a numbered, additive & idempotent migration function."""
    def deco(fn):
        if number in _MIGRATIONS:
            raise ValueError(f"duplicate migration number: {number}")
        _MIGRATIONS[number] = fn
        return fn
    return deco


def _table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [col[1] for col in cursor.fetchall()]


def _add_column(cursor, table, column, ddl):
    """Idempotent ALTER TABLE ADD COLUMN."""
    if column not in _table_columns(cursor, table):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _get_version(cursor):
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER NOT NULL DEFAULT 0)"
    )
    cursor.execute("SELECT version FROM schema_version LIMIT 1")
    row = cursor.fetchone()
    if row is None:
        cursor.execute("INSERT INTO schema_version (version) VALUES (0)")
        return 0
    return row[0]


def _set_version(cursor, version):
    cursor.execute("UPDATE schema_version SET version = ?", (version,))


def _backup_db_file():
    """Copy the current DB file into backups/ before migrating (once per run)."""
    global _BACKUP_CREATED
    if _BACKUP_CREATED:
        return None
    _BACKUP_CREATED = True
    db_path = config.DB_NAME
    if not db_path or not os.path.exists(db_path):
        return None
    # Skip empty files (sqlite3.connect creates a 0-byte file lazily).
    if os.path.getsize(db_path) == 0:
        return None
    backup_dir = config.BACKUP_DIR
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(db_path)
    dest = os.path.join(backup_dir, f"{base}.{stamp}.bak")
    shutil.copy2(db_path, dest)
    # Keep only the 20 most recent backups.
    pattern = os.path.join(backup_dir, f"{base}.*.bak")
    old = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)[20:]
    for stale in old:
        try:
            os.remove(stale)
        except OSError:
            pass
    return dest


def migrate(conn=None):
    """Apply every pending migration in order. Returns the final version."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cursor = conn.cursor()
        current = _get_version(cursor)
        conn.commit()
        pending = sorted(n for n in _MIGRATIONS if n > current)
        if pending:
            _backup_db_file()
        for number in pending:
            _MIGRATIONS[number](cursor)
            _set_version(cursor, number)
            conn.commit()
        return _get_version(cursor)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Base tables (legacy + Phase 1 spec)
# ---------------------------------------------------------------------------

@migration(1)
def _m1_base_tables(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            subtitle TEXT NOT NULL,
            image_url TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            price TEXT NOT NULL,
            discount TEXT,
            duration TEXT,
            image_url TEXT NOT NULL,
            html_code TEXT NOT NULL,
            is_top10 INTEGER DEFAULT 0,
            FOREIGN KEY (package_id) REFERENCES packages (id)
        )
    ''')
    for col, ddl in (
        ('html_code', "TEXT DEFAULT ''"),
        ('discount', "TEXT DEFAULT ''"),
        ('duration', "TEXT DEFAULT ''"),
        ('is_top10', "INTEGER DEFAULT 0"),
    ):
        _add_column(cursor, 'templates', col, ddl)

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS media_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')


@migration(2)
def _m2_tiers(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            subtitle TEXT NOT NULL,
            positioning TEXT NOT NULL DEFAULT '',
            active_days INTEGER NOT NULL,
            price_label TEXT NOT NULL DEFAULT '',
            limits_json TEXT NOT NULL DEFAULT '{}',
            features_json TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')


@migration(3)
def _m3_clients_and_templates(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            whatsapp TEXT NOT NULL DEFAULT '',
            email TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # New template columns; tier_id maps from the legacy package_id.
    for col, ddl in (
        ('code', "TEXT DEFAULT ''"),
        ('tier_id', "INTEGER"),
        ('source_dir', "TEXT DEFAULT ''"),
        ('schema_json', "TEXT DEFAULT '{}'"),
        ('preview_json', "TEXT DEFAULT '{}'"),
        ('active', "INTEGER NOT NULL DEFAULT 1"),
    ):
        _add_column(cursor, 'templates', col, ddl)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_templates_tier ON templates (tier_id)"
    )


@migration(4)
def _m4_orders_and_invitations(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            client_id INTEGER,
            tier_id INTEGER NOT NULL,
            template_id INTEGER,
            status TEXT NOT NULL DEFAULT 'new',
            buyer_name TEXT NOT NULL DEFAULT '',
            whatsapp TEXT NOT NULL DEFAULT '',
            amount TEXT NOT NULL DEFAULT '',
            payment_method TEXT DEFAULT '',
            expires_at TEXT,
            paid_at TEXT,
            published_at TEXT,
            expired_at TEXT,
            cancelled_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients (id),
            FOREIGN KEY (tier_id) REFERENCES tiers (id),
            FOREIGN KEY (template_id) REFERENCES templates (id)
        )
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status)"
    )

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            couple_names TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT '',
            event_time TEXT NOT NULL DEFAULT '',
            venue_name TEXT NOT NULL DEFAULT '',
            venue_address TEXT NOT NULL DEFAULT '',
            maps_url TEXT DEFAULT '',
            music_url TEXT DEFAULT '',
            cover_image TEXT DEFAULT '',
            background_image TEXT DEFAULT '',
            colors_json TEXT DEFAULT '{}',
            fonts_json TEXT DEFAULT '{}',
            content_json TEXT DEFAULT '{}',
            custom BOOLEAN NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'draft',
            published_at TEXT,
            expires_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        )
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_invitations_order ON invitations (order_id)"
    )

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invitation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invitation_id INTEGER NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            day_label TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT '',
            event_time TEXT NOT NULL DEFAULT '',
            venue_name TEXT NOT NULL DEFAULT '',
            venue_address TEXT NOT NULL DEFAULT '',
            maps_url TEXT DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (invitation_id) REFERENCES invitations (id)
        )
    ''')


@migration(5)
def _m5_content_tables(cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'image',
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            content_type TEXT DEFAULT '',
            owner_type TEXT DEFAULT '',
            owner_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invitation_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            personal_code TEXT NOT NULL UNIQUE,
            note TEXT DEFAULT '',
            invited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_visit_at TIMESTAMP,
            visit_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (invitation_id) REFERENCES invitations (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rsvps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invitation_id INTEGER NOT NULL,
            guest_name TEXT NOT NULL,
            attendance TEXT NOT NULL DEFAULT 'unknown',
            pax_count INTEGER NOT NULL DEFAULT 1,
            message TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (invitation_id) REFERENCES invitations (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS wishes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invitation_id INTEGER NOT NULL,
            guest_name TEXT NOT NULL,
            wish_text TEXT NOT NULL,
            wish_type TEXT NOT NULL DEFAULT 'present',
            attended BOOLEAN NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (invitation_id) REFERENCES invitations (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            method TEXT NOT NULL DEFAULT '',
            amount TEXT NOT NULL DEFAULT '',
            reference TEXT DEFAULT '',
            proof_path TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            verified_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            invitation_id INTEGER,
            requester_name TEXT NOT NULL DEFAULT '',
            whatsapp TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            quoted_price TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            read BOOLEAN NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')


@migration(6)
def _m6_checkout(cursor):
    """Phase 4: payment settings + checkout columns on orders.

    * ``settings`` holds admin-editable payment details (bank name/number/
      holder, QRIS image path) as key/value rows.
    * ``orders.payment_unique_code`` is the 3-digit code unique among orders
      awaiting payment; ``payment_deadline`` drives the lazy 24h cancel.
    * ``orders.discount`` snapshots the template discount at checkout time.
    """
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    _add_column(cursor, 'orders', 'payment_unique_code', "TEXT")
    _add_column(cursor, 'orders', 'payment_deadline', "TEXT")
    _add_column(cursor, 'orders', 'discount', "TEXT DEFAULT ''")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_code"
        " ON orders (payment_unique_code)"
        " WHERE payment_unique_code IS NOT NULL"
    )


@migration(7)
def _m7_order_events(cursor):
    """Phase 5: audit log for every order status change / admin action.

    One row per event: who (actor), when (created_at), from_status -> to_status
    plus a free-form action label and note (e.g. rejection reason).
    """
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            action TEXT NOT NULL DEFAULT '',
            from_status TEXT NOT NULL DEFAULT '',
            to_status TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        )
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_order_events_order"
        " ON order_events (order_id)"
    )


@migration(8)
def _m8_phase7(cursor):
    """Phase 7: live wish moderation + guest list tracking columns.

    * ``wishes.hidden``   — couple can hide/unhide wishes (Gold feature).
    * ``guests.opened_at`` / ``guests.checked_in_at`` — personal-link open
      tracking and QR check-in by the usher.
    All additive & idempotent via guarded ALTER TABLE.
    """
    _add_column(cursor, 'wishes', 'hidden', "INTEGER NOT NULL DEFAULT 0")
    _add_column(cursor, 'guests', 'opened_at', "TIMESTAMP")
    _add_column(cursor, 'guests', 'checked_in_at', "TIMESTAMP")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_wishes_invitation"
        " ON wishes (invitation_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_rsvps_invitation"
        " ON rsvps (invitation_id)"
    )


@migration(9)
def _m9_tier_matrix_fix(cursor):
    """Phase 7.5 (B1): repair the PRODUCTION BUG in the seeded tier matrix.

    Silver was wrongly granted the ``qr`` feature and had limits below spec
    (gallery 5 / video 0 / events 1 instead of 10 / 1 / 2); Platinum had
    gold-level limits instead of 'no limits'. The authoritative fix lives in
    services/tiers.py; this migration rewrites tiers.features_json and
    tiers.limits_json for every existing row from that catalogue so already-
    migrated databases converge on the correct values. Additive & idempotent
    (pure UPDATE, no schema change).
    """
    import json
    import services.tiers as tiers_svc

    try:
        rows = cursor.execute("SELECT code FROM tiers").fetchall()
    except sqlite3.Error:      # pragma: no cover - tiers table always exists here
        return
    for (code,) in rows:
        if code not in getattr(tiers_svc, "_BY_CODE", {}):
            continue
        features = [f for f in _FULL_FEATURES
                    if tiers_svc.has_feature(code, f)]
        limits = {k: tiers_svc.limit(code, k) for k in tiers_svc.KNOWN_LIMITS}
        cursor.execute(
            "UPDATE tiers SET features_json = ?, limits_json = ?,"
            " active_days = ?, name = ?, subtitle = ?, positioning = ?,"
            " price_label = ?, sort_order = ? WHERE code = ?",
            (json.dumps(features), json.dumps(limits),
             tiers_svc.active_days(code),
             tiers_svc.get_tier(code)["name"],
             tiers_svc.get_tier(code)["subtitle"],
             tiers_svc.get_tier(code)["positioning"],
             tiers_svc.get_tier(code)["price_label"],
             tiers_svc.get_tier(code)["sort_order"], code))


@migration(10)
def _m10_payment_proof_columns(cursor):
    """Phase 3.2: bookkeeping columns for customer payment-proof submission.

    The legacy ``payments`` table (migration 1) predates the proof-upload
    flow; add the missing columns idempotently. Existing rows keep their
    values; new submissions record submitted_at + proof provenance
    (original name / mime / size packed into ``reference`` as PROOF:<json>).
    Additive & idempotent — no data rewrite, existing orders/payments stay
    valid.
    """
    _add_column(cursor, 'payments', 'submitted_at', "TIMESTAMP")
    _add_column(cursor, 'payments', 'updated_at', "TIMESTAMP")


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

SAMPLE_HTML = """<!DOCTYPE html><html lang="id"><head><meta charset="UTF-8"><title>Undangan Pernikahan</title><style>body{background:#fdfbf7;font-family:serif;text-align:center;padding:40px;color:#333;}h1{color:#b8860b;font-size:36px;margin-bottom:5px;}.editable{border:1px dashed #d4af37;padding:5px;display:inline-block;margin:5px;background:#fff;min-width:150px;}</style></head><body><h1>The Wedding of</h1><h2 contenteditable="true" class="editable">Rian & Siska</h2><p>Hari/Tanggal: <span contenteditable="true" class="editable">12 Desember 2026</span></p><p>Bertempat di: <span contenteditable="true" class="editable">Gedung Kencana Cirebon</span></p><hr style="width:50%;margin:20px auto;"><p>Merupakan suatu kehormatan dan kebahagiaan bagi kami apabila Bapak/Ibu berkenan hadir.</p></body></html>"""

# Full feature set; each tier gets a subset (see PLAYBOOK section 1 matrix).
_FULL_FEATURES = [
    "rsvp", "wishes", "gift", "personal_link", "music", "countdown", "map",
    "live_rsvp", "guest_list", "qr", "multi_event", "custom_slug",
    "custom_colors", "custom_fonts", "custom_cover", "custom_background",
    "custom_sections", "custom_wording", "og_preview", "custom_design",
]


def _seed_data(cursor):
    import json

    # --- tiers -------------------------------------------------------------
    import services.tiers as tiers_svc

    cursor.execute("SELECT COUNT(*) FROM tiers")
    if cursor.fetchone()[0] == 0:
        tier_rows = []
        for t in tiers_svc.TIERS:
            features = [f for f in _FULL_FEATURES if tiers_svc.has_feature(t["code"], f)]
            limits = {k: tiers_svc.limit(t["code"], k) for k in tiers_svc.KNOWN_LIMITS}
            tier_rows.append((
                t["code"], t["name"], t["subtitle"], t["positioning"],
                t["active_days"], t["price_label"],
                json.dumps(limits), json.dumps(features), t["sort_order"],
            ))
        cursor.executemany(
            "INSERT INTO tiers (code, name, subtitle, positioning, active_days,"
            " price_label, limits_json, features_json, sort_order)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", tier_rows)

    # Keep names/subtitles/positioning/days/features/limits in sync with the
    # catalogue (idempotent; also refreshes rows seeded by older versions).
    for t in tiers_svc.TIERS:
        features = [f for f in _FULL_FEATURES if tiers_svc.has_feature(t["code"], f)]
        limits = {k: tiers_svc.limit(t["code"], k) for k in tiers_svc.KNOWN_LIMITS}
        cursor.execute(
            "UPDATE tiers SET name = ?, subtitle = ?, positioning = ?,"
            " active_days = ?, price_label = ?, limits_json = ?,"
            " features_json = ?, sort_order = ? WHERE code = ?",
            (t["name"], t["subtitle"], t["positioning"], t["active_days"],
             t["price_label"], json.dumps(limits), json.dumps(features),
             t["sort_order"], t["code"]))

    # --- packages (legacy table kept) --------------------------------------
    cursor.execute("SELECT COUNT(*) FROM packages")
    if cursor.fetchone()[0] == 0:
        initial_pkgs = [
            ('Silver Package', 'Digital Undangan Basic', 'https://images.unsplash.com/photo-1519741497674-611481863552?auto=format&fit=crop&w=600&q=80'),
            ('Gold Package', 'Custom Domain + RSVP', 'https://images.unsplash.com/photo-1511285560929-80b456fea0bc?auto=format&fit=crop&w=600&q=80'),
            ('Platinum VIP', 'All-in-One Exclusive', 'https://images.unsplash.com/photo-1465495976277-4387d4b0b4c6?auto=format&fit=crop&w=600&q=80'),
        ]
        cursor.executemany(
            'INSERT INTO packages (name, subtitle, image_url) VALUES (?, ?, ?)',
            initial_pkgs)

    # --- map package_id -> tier_id ------------------------------------------
    pkg_to_tier = {'Silver Package': 'silver', 'Gold Package': 'gold',
                   'Platinum VIP': 'platinum'}
    legacy_dur_to_tier = {'Aktif 6 Bulan': 'silver', 'Aktif 1 Tahun': 'gold',
                          'Aktif Selamanya': 'platinum'}
    tier_ids = dict(cursor.execute('SELECT code, id FROM tiers').fetchall())

    def _tid_for(pid):
        """Tier row whose package name matches the given legacy package id."""
        cursor.execute('SELECT name FROM packages WHERE id = ?', (pid,))
        row = cursor.fetchone()
        code = pkg_to_tier.get(row[0]) if row else None
        return tier_ids.get(code) if code else None

    # Backfill every template that still lacks a tier mapping.
    cursor.execute('SELECT id, package_id, duration FROM templates')
    for trow_id, pkg_id_, dur_text in cursor.fetchall():
        tid = _tid_for(pkg_id_)
        if tid is None and dur_text:
            code = legacy_dur_to_tier.get(str(dur_text).strip())
            if code:
                tid = tier_ids.get(code)
        if tid is not None:
            cursor.execute(
                'UPDATE templates SET tier_id = ?'
                ' WHERE id = ? AND (tier_id IS NULL OR tier_id = 0)',
                (tid, trow_id))

    # --- templates -----------------------------------------------------------
    cursor.execute("SELECT COUNT(*) FROM templates")
    if cursor.fetchone()[0] == 0:
        def _pkg_id_for(code):
            want = {v: k for k, v in pkg_to_tier.items()}[code]
            cursor.execute('SELECT id FROM packages WHERE name = ?', (want,))
            row = cursor.fetchone()
            return row[0] if row else 1
        silver_pkg, gold_pkg = _pkg_id_for('silver'), _pkg_id_for('gold')
        # Phase 1 resolver: first data-driven template converted to
        # templates_html/silver/s01-classic-matcha/ (source_dir is relative to
        # config.TEMPLATES_HTML_DIR). No html_code — the renderer reads the
        # files from disk instead. It takes the first seed slot so existing
        # tests that reference template ids 1..4 keep their legacy semantics.
        matcha_schema = _template_source_json('s01-classic-matcha', 'schema.json')
        matcha_preview = _template_source_json('s01-classic-matcha', 'preview.json')
        initial_tmpls = []
        if matcha_schema is not None:
            initial_tmpls.append((
                silver_pkg, 'Classic Matcha', 'Rp 150.000', '',
                tier_ids.get('silver'),
                'https://images.unsplash.com/photo-1520854221256-17451cc331bf?auto=format&fit=crop&w=400&q=80',
                '', 0, 's01-classic-matcha', 'silver/s01-classic-matcha',
                matcha_schema, matcha_preview))
        initial_tmpls += [
            (silver_pkg, 'Classic Floral', 'Rp 150.000', 'Diskon 10%', tier_ids.get('silver'), 'https://images.unsplash.com/photo-1520854221256-17451cc331bf?auto=format&fit=crop&w=400&q=80', SAMPLE_HTML, 1, 'classic-floral', '', '{}', '{}'),
            (silver_pkg, 'Minimalist Monokrom', 'Rp 175.000', '', tier_ids.get('silver'), 'https://images.unsplash.com/photo-1519225421980-715cb0215aed?auto=format&fit=crop&w=400&q=80', SAMPLE_HTML, 1, 'minimalist-monokrom', '', '{}', '{}'),
            (gold_pkg, 'Rustic Wood', 'Rp 300.000', 'Promo Launching', tier_ids.get('gold'), 'https://images.unsplash.com/photo-1511795409834-ef04bbd61622?auto=format&fit=crop&w=400&q=80', SAMPLE_HTML, 1, 'rustic-wood', '', '{}', '{}'),
            (gold_pkg, 'Modern Gold Luxury', 'Rp 350.000', 'Best Seller', tier_ids.get('gold'), 'https://images.unsplash.com/photo-1532712938310-34cb3982ef74?auto=format&fit=crop&w=400&q=80', SAMPLE_HTML, 1, 'modern-gold-luxury', '', '{}', '{}'),
        ]
        cursor.executemany(
            'INSERT INTO templates (package_id, name, price, discount, tier_id,'
            ' image_url, html_code, is_top10, code, source_dir,'
            ' schema_json, preview_json)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', initial_tmpls)


def _template_source_json(code, filename):
    """Mirror of a converted template file for DB seeding; None when the
    source dir does not exist (e.g. a stripped-down checkout)."""
    import json
    path = os.path.join(config.TEMPLATES_HTML_DIR, 'silver', code, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return None
    # Store compactly but validate it parses (schema/preview must be JSON).
    try:
        json.loads(content)
    except ValueError:
        return None
    return content


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _seed_settings(cursor):
    """Idempotent defaults for the settings table (admin can edit later)."""
    defaults = [
        ("bank_name", "BCA"),
        ("bank_number", "1234567890"),
        ("bank_holder", "SUKA MOTO"),
        ("qris_path", ""),
    ]
    for key, value in defaults:
        cursor.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO NOTHING", (key, value))


def init_db():
    """Back up (if needed), migrate and seed the database. Idempotent."""
    conn = get_conn()
    try:
        migrate(conn)
        cursor = conn.cursor()
        _seed_data(cursor)
        _seed_settings(cursor)
        conn.commit()
    finally:
        conn.close()
