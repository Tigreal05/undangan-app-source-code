"""Shared pytest fixtures: every test runs against an isolated tmp app instance."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point config at a throwaway DB/upload dir and a known admin password."""
    import config
    import security

    db_path = str(tmp_path / "test.db")
    upload_dir = str(tmp_path / "static_uploads")
    homepage = str(tmp_path / "homepage.html")
    backup_dir = str(tmp_path / "backups")

    monkeypatch.setattr(config, "DB_NAME", db_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(config, "HOMEPAGE_FILE", homepage)
    monkeypatch.setattr(config, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret-admin-pw")
    monkeypatch.setattr(config, "SECRET_KEY", "unit-test-secret")

    # Handlers import get_conn from the db module; it reads config lazily.
    def _get_conn():
        import sqlite3
        return sqlite3.connect(config.DB_NAME)

    monkeypatch.setattr("db.get_conn", _get_conn)

    security.reset_login_attempts()

    # The one-shot DB backup guard must be reset per test as well.
    import db
    monkeypatch.setattr(db, "_BACKUP_CREATED", False)
    yield
