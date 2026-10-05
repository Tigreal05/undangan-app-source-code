"""SUKA MOTO invitation app — bootstrap only.

Application logic lives in:
  config.py        environment-driven settings (ADMIN_PASSWORD, SECRET_KEY, ...)
  db.py            SQLite schema, seeding and connection helper
  security.py      signed session cookie, admin auth middleware, login rate
                   limit, upload validation helpers
  routes/public.py public pages (index, templates, template-action, demo,
                   editor, checkout, guestbook)
  routes/admin.py  /admin/login + /admin/logout, dashboard and every
                   /admin/* POST action

Run with:  python app.py
"""
from aiohttp import web

import config
import security
from db import init_db
from routes import admin as admin_routes
from routes import public as public_routes


def create_app() -> web.Application:
    """Build the aiohttp application (also used by the test-suite)."""
    config.ensure_dirs()
    init_db()

    app = web.Application(middlewares=[security.admin_auth_middleware])

    public_routes.setup(app)
    admin_routes.setup(app)

    app.router.add_static('/static_uploads/', path=config.UPLOAD_DIR, name='static_uploads')
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='0.0.0.0', port=9000)
