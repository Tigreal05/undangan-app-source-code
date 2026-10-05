# Undangan Digital — aiohttp app

## Requirements / Python compatibility

- **python_requires: >=3.10** (the owner runs Python 3.10; the sandbox/test CI runs 3.12).
- All source is kept compatible with Python 3.10 syntax: no `type X = ...` aliases,
  no PEP 695 generics (`def f[T](...)`), no `except*`, and no f-string nesting that
  only works on 3.12+.
- Install dependencies (versions pinned to what has been tested):

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## Running

```bash
cp .env.example .env   # then fill in ADMIN_PASSWORD / SECRET_KEY etc.
venv/bin/python app.py
```

## Database

`undangan.db` is **created automatically** on first boot: `create_app()` in `app.py`
calls `db.init_db()`, which backs up any existing DB file to `backups/` and then runs
`db.migrate()` (numbered, idempotent migrations tracked in the `schema_version` table)
plus seeding. You never need to create or import the SQLite file manually; it is
gitignored.

## Tests

```bash
venv/bin/python -m pytest tests/
```
