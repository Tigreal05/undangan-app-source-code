"""SQLite database helpers: schema creation, seeding and connection factory."""
import sqlite3

from config import DB_NAME


def get_conn():
    """Return a new SQLite connection (same behavior as the original app)."""
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_conn()
    cursor = conn.cursor()

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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS media_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute("PRAGMA table_info(templates)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'html_code' not in columns:
        cursor.execute("ALTER TABLE templates ADD COLUMN html_code TEXT DEFAULT ''")
    if 'discount' not in columns:
        cursor.execute("ALTER TABLE templates ADD COLUMN discount TEXT DEFAULT ''")
    if 'duration' not in columns:
        cursor.execute("ALTER TABLE templates ADD COLUMN duration TEXT DEFAULT ''")
    if 'is_top10' not in columns:
        cursor.execute("ALTER TABLE templates ADD COLUMN is_top10 INTEGER DEFAULT 0")

    cursor.execute('SELECT COUNT(*) FROM packages')
    if cursor.fetchone()[0] == 0:
        initial_pkgs = [
            ('Silver Package', 'Digital Undangan Basic', 'https://images.unsplash.com/photo-1519741497674-611481863552?auto=format&fit=crop&w=600&q=80'),
            ('Gold Package', 'Custom Domain + RSVP', 'https://images.unsplash.com/photo-1511285560929-80b456fea0bc?auto=format&fit=crop&w=600&q=80'),
            ('Platinum VIP', 'All-in-One Exclusive', 'https://images.unsplash.com/photo-1465495976277-4387d4b0b4c6?auto=format&fit=crop&w=600&q=80')
        ]
        cursor.executemany('INSERT INTO packages (name, subtitle, image_url) VALUES (?, ?, ?)', initial_pkgs)
        conn.commit()

    cursor.execute('SELECT COUNT(*) FROM templates')
    if cursor.fetchone()[0] == 0:
        sample_html = """<!DOCTYPE html><html lang="id"><head><meta charset="UTF-8"><title>Undangan Pernikahan</title><style>body{background:#fdfbf7;font-family:serif;text-align:center;padding:40px;color:#333;}h1{color:#b8860b;font-size:36px;margin-bottom:5px;}.editable{border:1px dashed #d4af37;padding:5px;display:inline-block;margin:5px;background:#fff;min-width:150px;}</style></head><body><h1>The Wedding of</h1><h2 contenteditable="true" class="editable">Rian & Siska</h2><p>Hari/Tanggal: <span contenteditable="true" class="editable">12 Desember 2026</span></p><p>Bertempat di: <span contenteditable="true" class="editable">Gedung Kencana Cirebon</span></p><hr style="width:50%;margin:20px auto;"><p>Merupakan suatu kehormatan dan kebahagiaan bagi kami apabila Bapak/Ibu berkenan hadir.</p></body></html>"""
        initial_tmpls = [
            (1, 'Classic Floral', 'Rp 150.000', 'Diskon 10%', 'Aktif 6 Bulan', 'https://images.unsplash.com/photo-1520854221256-17451cc331bf?auto=format&fit=crop&w=400&q=80', sample_html, 1),
            (1, 'Minimalist Monokrom', 'Rp 175.000', '', 'Aktif 6 Bulan', 'https://images.unsplash.com/photo-1519225421980-715cb0215aed?auto=format&fit=crop&w=400&q=80', sample_html, 1),
            (2, 'Rustic Wood', 'Rp 300.000', 'Promo Launching', 'Aktif 1 Tahun', 'https://images.unsplash.com/photo-1511795409834-ef04bbd61622?auto=format&fit=crop&w=400&q=80', sample_html, 1),
            (2, 'Modern Gold Luxury', 'Rp 350.000', 'Best Seller', 'Aktif Selamanya', 'https://images.unsplash.com/photo-1532712938310-34cb3982ef74?auto=format&fit=crop&w=400&q=80', sample_html, 1),
        ]
        cursor.executemany('INSERT INTO templates (package_id, name, price, discount, duration, image_url, html_code, is_top10) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', initial_tmpls)
        conn.commit()

    conn.close()
