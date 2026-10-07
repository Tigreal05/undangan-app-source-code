"""Template resolver tests (Phase 1).

Covers: new-source priority, legacy fallback, unknown templates, path
traversal rejection, strict-mode errors and preview data loading.
"""
import json
import os
import sqlite3

import pytest

import config
import db as db_mod
from services import template_resolver as resolver


@pytest.fixture()
def seeded(tmp_path):
    """A migrated+seeded DB whose template rows live in ``tmp_path``."""
    conn = sqlite3.connect(str(tmp_path / "resolver.db"))
    db_mod.migrate(conn)
    db_mod._seed_data(conn.cursor())
    conn.commit()
    conn.close()
    return str(tmp_path / "resolver.db")


@pytest.fixture()
def use_db(seeded, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", seeded)


def _row(cursor, tid):
    cursor.execute(resolver.TEMPLATE_SELECT.format(clause="t.id = ?"), (tid,))
    return cursor.fetchone()


# ---------------------------------------------------------------------------
# TEST 1 — valid new template
# ---------------------------------------------------------------------------

def test_resolve_new_template(use_db):
    conn = sqlite3.connect(config.DB_NAME)
    try:
        row = _row(conn.cursor(), 1)          # s01-classic-matcha (seeded first)
        resolved = resolver.resolve_template(row)
    finally:
        conn.close()
    assert resolved["source"] == "new"
    assert resolved["code"] == "s01-classic-matcha"
    assert resolved["tier_code"] == "silver"
    assert resolved["template_dir"].endswith(
        os.path.join("templates_html", "silver", "s01-classic-matcha"))
    assert os.path.isfile(resolved["template_path"])
    assert os.path.basename(resolved["template_path"]) == "template.html"
    assert resolved["schema_path"].endswith("schema.json")
    assert resolved["preview_path"].endswith("preview.json")
    # Resolver never returns raw HTML content — only locations + the column.
    assert "<html" not in resolved["template_dir"]


def test_resolve_by_code_and_tier(use_db):
    resolved = resolver.resolve_template_by_code("s01-classic-matcha", "silver")
    assert resolved["source"] == "new"
    assert resolved["id"] == 1


def test_resolve_by_id(use_db):
    assert resolver.resolve_template_by_id(1)["source"] == "new"


# ---------------------------------------------------------------------------
# TEST 2 — unknown template
# ---------------------------------------------------------------------------

def test_unknown_id_raises_not_found(use_db):
    with pytest.raises(resolver.TemplateNotFound):
        resolver.resolve_template_by_id(9999)


def test_unknown_code_raises_not_found(use_db):
    with pytest.raises(resolver.TemplateNotFound):
        resolver.resolve_template_by_code("no-such-template")


# ---------------------------------------------------------------------------
# TEST 3 — path traversal attempts are rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "../../etc", "..%2f..", "../evil", "/etc/passwd", "a/b", "SILVER!", "",
])
def test_validate_slug_rejects_unsafe(bad):
    with pytest.raises(resolver.InvalidTemplateSlug):
        resolver.validate_slug(bad)


def test_resolve_by_code_traversal_rejected(use_db):
    with pytest.raises(resolver.InvalidTemplateSlug):
        resolver.resolve_template_by_code("../../etc/passwd")


def test_row_with_traversal_source_dir_stays_inside_root(use_db, tmp_path):
    """A malicious source_dir in the DB must never escape TEMPLATES_HTML_DIR."""
    evil = str(tmp_path / "escape")
    os.makedirs(evil, exist_ok=True)
    with open(os.path.join(evil, "template.html"), "w") as f:
        f.write("<html><body>ESCAPED</body></html>")
    conn = sqlite3.connect(config.DB_NAME)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE templates SET source_dir = ?, code = '', tier_id = NULL,"
            " html_code = ? WHERE id = 1",
            (f"{evil}/../escape", "<html><body>LEGACY</body></html>"))
        conn.commit()
        resolved = resolver.resolve_template(_row(cur, 1))
    finally:
        conn.close()
    # Absolute/escaping source_dir is ignored → falls back to legacy (empty
    # html_code here), and nothing outside the root was ever touched.
    assert resolved["source"] == "legacy"
    assert resolved["template_dir"] == "" or resolved["template_dir"].startswith(
        os.path.realpath(config.TEMPLATES_HTML_DIR))


def test_row_with_relative_traversal_code_stays_inside_root(use_db):
    conn = sqlite3.connect(config.DB_NAME)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE templates SET code = ?, tier_id = 1 WHERE id = 1",
                    ("../../etc/cron.d",))
        conn.commit()
        resolved = resolver.resolve_template(_row(cur, 1))
    finally:
        conn.close()
    assert resolved["source"] == "legacy"
    assert not resolved["template_dir"].startswith("/etc")


# ---------------------------------------------------------------------------
# TEST 4 — legacy fallback
# ---------------------------------------------------------------------------

def test_legacy_template_falls_back(use_db):
    conn = sqlite3.connect(config.DB_NAME)
    try:
        row = _row(conn.cursor(), 2)          # classic-floral: html_code only
        resolved = resolver.resolve_template(row)
    finally:
        conn.close()
    assert resolved["source"] == "legacy"
    assert resolved["template_dir"] == ""
    assert "DOCTYPE" in resolved["html_code"]


def test_strict_mode_errors():
    # Directory exists but template.html missing → TemplateInvalid.
    missing = {
        "id": 7, "name": "x", "code": "ghost-tmpl", "tier_id": 1,
        "tier_code": "silver", "source_dir": "", "schema_json": "{}",
        "preview_json": "{}", "html_code": "<html></html>",
    }
    real_dir = os.path.join(config.TEMPLATES_HTML_DIR, "silver", "ghost-tmpl")
    os.makedirs(real_dir, exist_ok=True)
    try:
        assert resolver.resolve_template(missing)["source"] == "legacy"
        with pytest.raises(resolver.TemplateInvalid):
            resolver.resolve_template(missing, strict=True)
    finally:
        os.rmdir(real_dir)

    # No new source AND no html_code → TemplateSourceUnavailable.
    empty = dict(missing, code="nowhere-tmpl", html_code="")
    with pytest.raises(resolver.TemplateSourceUnavailable):
        resolver.resolve_template(empty, strict=True)


# ---------------------------------------------------------------------------
# Preview data
# ---------------------------------------------------------------------------

def test_load_preview_data_from_file(use_db):
    resolved = resolver.resolve_template_by_id(1)
    data = resolver.load_preview_data(resolved)
    assert data.get("title")            # comes from preview.json on disk
    assert isinstance(data, dict)


def test_load_preview_data_db_fallback(tmp_path, monkeypatch):
    """When preview.json is absent the DB preview_json column is used."""
    monkeypatch.setattr(config, "TEMPLATES_HTML_DIR", str(tmp_path / "none"))
    row = {
        "id": 8, "name": "x", "code": "c", "tier_id": 1, "tier_code": "silver",
        "source_dir": "", "schema_json": "{}",
        "preview_json": json.dumps({"title": "From DB"}), "html_code": "",
    }
    resolved = resolver.resolve_template(row)
    assert resolver.load_preview_data(resolved) == {"title": "From DB"}
