"""Phase 2 — schema-driven invitation editor tests (A–S + transaction).

Covers:
* /editor new-template branch via template resolver (schema-driven form)
* legacy /editor fallback untouched
* preview.json as read-only initial data; draft content_json takes precedence
* POST /editor/preview transient rendering (no DB writes, no orders)
* POST /editor/save atomic draft order+invitation, idempotent per context
* server-side validation: required / select / date / unexpected keys dropped
* security: no arbitrary filesystem path control, malformed payloads rejected
"""
import json
import os

import pytest

import config
from app import create_app

NEW_ID = 1      # s01-classic-matcha — converted template (templates_html/)
LEGACY_ID = 2   # classic-floral — html_code only

VALID_DATA = {
    "title": "Witan & Aiko",
    "groom.full_name": "Witan Prayoga, S.T.",
    "bride.full_name": "Aiko Sakura, S.E.",
    "date_iso": "2026-12-12",
    "time_range": "09.00 WIB",
    "events": [{"label": "Akad Nikah", "btn_class": "btn-matcha"}],
}


def _repo_preview_path():
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates_html", "silver", "s01-classic-matcha", "preview.json")


def _db_counts(client_app):
    import sqlite3
    conn = sqlite3.connect(config.DB_NAME)
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM invitations")
        n_inv = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM orders")
        n_ord = c.fetchone()[0]
        return n_inv, n_ord
    finally:
        conn.close()


async def _save(client, data, extra=None):
    body = {"template_id": NEW_ID, "data": data}
    if extra:
        body.update(extra)
    resp = await client.post("/editor/save", json=body)
    return resp, await resp.json()


@pytest.mark.asyncio
async def test_editor_new_template_returns_200(aiohttp_client):
    """TEST A: new-template /editor renders the schema-driven page."""
    client = await aiohttp_client(create_app())
    resp = await client.get(f"/editor?id={NEW_ID}")
    body = await resp.text()
    assert resp.status == 200
    assert "__EDITOR_FORM__" in body                     # schema metadata injected
    assert "Simpan Draft" in body                        # draft save button
    assert 'id="ed-frame"' in body                       # live preview iframe
    # New editor is a DATA editor: no contenteditable-as-data architecture.
    assert "contenteditable" not in body


@pytest.mark.asyncio
async def test_editor_legacy_template_still_works(aiohttp_client):
    """TEST B + Q: legacy templates keep the old contenteditable behaviour."""
    client = await aiohttp_client(create_app())
    resp = await client.get(f"/editor?id={LEGACY_ID}")
    body = await resp.text()
    assert resp.status == 200
    assert "sukamoto-builder-bar" in body                # legacy editor banner
    assert "Lanjut ke Pembayaran" in body                # legacy checkout link


@pytest.mark.asyncio
async def test_editor_unknown_template_404(aiohttp_client):
    client = await aiohttp_client(create_app())
    resp = await client.get("/editor?id=99999")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_editor_loads_schema_form_meta(aiohttp_client):
    """TEST C: schema.json drives form generation (labels/types present)."""
    client = await aiohttp_client(create_app())
    resp = await client.get(f"/editor?id={NEW_ID}")
    body = await resp.text()
    # Schema labels reach the client through the embedded form metadata blob.
    assert "Judul Nama" in body                          # couple.title label
    assert "Nama Lengkap + Gelar" in body                # groom/bride full_name
    assert "Daftar Acara" in body                        # repeatable events
    assert "btn-outline-matcha" in body                  # select options from schema


@pytest.mark.asyncio
async def test_editor_initial_data_from_preview_json(aiohttp_client):
    """TEST D: with no draft, preview.json seeds the editor blob."""
    client = await aiohttp_client(create_app())
    resp = await client.get(f"/editor?id={NEW_ID}")
    body = await resp.text()
    assert "Rian &amp; Siska" in body or "Rian & Siska" in body
    assert "Gedung Kencana Cirebon" in body


@pytest.mark.asyncio
async def test_preview_json_never_modified(aiohttp_client, tmp_path):
    """TEST E: editing/previewing/saving never touches preview.json."""
    path = _repo_preview_path()
    before = open(path, "rb").read()
    client = await aiohttp_client(create_app())
    await client.get(f"/editor?id={NEW_ID}")
    await client.post("/editor/preview",
                      json={"template_id": NEW_ID, "data": VALID_DATA})
    resp, j = await _save(client, VALID_DATA)
    assert j["ok"]
    resp, j2 = await _save(client, VALID_DATA)          # repeated save
    assert j2["ok"]
    after = open(path, "rb").read()
    assert before == after


@pytest.mark.asyncio
async def test_draft_takes_precedence_over_preview(aiohttp_client):
    """Draft content_json wins over preview.json on re-open."""
    client = await aiohttp_client(create_app())
    _, j = await _save(client, VALID_DATA)
    inv_id = j["invitation_id"]
    resp = await client.get(f"/editor?id={NEW_ID}&draft={inv_id}")
    body = await resp.text()
    assert resp.status == 200
    assert "Witan Prayoga" in body                       # saved draft value
    assert "Rian Pratama" not in body                    # preview sample replaced


@pytest.mark.asyncio
async def test_required_validation(aiohttp_client):
    """TEST F: missing required fields are rejected server-side."""
    client = await aiohttp_client(create_app())
    resp, j = await _save(client, {"title": "", "date_iso": ""})
    assert resp.status == 422
    assert j["ok"] is False
    assert "title" in j["errors"]
    assert "date_iso" in j["errors"]
    n_inv, _ = _db_counts(client)
    assert n_inv == 0                                    # nothing persisted


@pytest.mark.asyncio
async def test_invalid_select_option_rejected(aiohttp_client):
    """TEST G: select values must exist in schema options."""
    client = await aiohttp_client(create_app())
    bad = dict(VALID_DATA)
    bad["events"] = [{"label": "Akad", "btn_class": "not-a-real-option"}]
    resp, j = await _save(client, bad)
    assert resp.status == 422
    assert any("btn_class" in k for k in j["errors"])


@pytest.mark.asyncio
async def test_unexpected_field_not_persisted(aiohttp_client):
    """TEST H: unknown keys are dropped and never rendered/stored."""
    client = await aiohttp_client(create_app())
    payload = dict(VALID_DATA)
    payload["evil_key"] = "<script>alert(1)</script>"
    payload["theme"] = {"bogus_css": "}body{display:none}"}
    resp, j = await _save(client, payload)
    # Unknown theme key that is not a canonical CSS var is rejected...
    assert resp.status == 422
    assert "theme.bogus_css" in j["errors"]
    # ...without it, unknown scalar keys are silently dropped.
    del payload["theme"]
    resp, j = await _save(client, payload)
    assert resp.status == 200 and j["ok"]
    import sqlite3
    conn = sqlite3.connect(config.DB_NAME)
    row = conn.execute("SELECT content_json FROM invitations WHERE id=?",
                       (j["invitation_id"],)).fetchone()
    conn.close()
    assert "evil_key" not in row[0]
    assert "alert(1)" not in row[0]


@pytest.mark.asyncio
async def test_invalid_date_format_rejected(aiohttp_client):
    client = await aiohttp_client(create_app())
    bad = dict(VALID_DATA)
    bad["date_iso"] = "12/2026/31"
    resp, j = await _save(client, bad)
    assert resp.status == 422
    assert "date_iso" in j["errors"]


@pytest.mark.asyncio
async def test_save_creates_draft_invitation_and_parent_order(aiohttp_client):
    """TEST I + J: one draft invitation + one minimal draft order, atomically."""
    client = await aiohttp_client(create_app())
    resp, j = await _save(client, VALID_DATA)
    assert resp.status == 200
    assert j["ok"] and isinstance(j["invitation_id"], int)
    import sqlite3
    conn = sqlite3.connect(config.DB_NAME)
    c = conn.cursor()
    inv = c.execute("SELECT order_id, state, content_json, slug FROM invitations"
                    " WHERE id=?", (j["invitation_id"],)).fetchone()
    assert inv is not None
    order_id, state = inv[0], inv[1]
    assert state == "draft"
    content = json.loads(inv[2])
    assert content["data"]["title"] == "Witan & Aiko"
    assert content["data"]["groom_full_name"] == "Witan Prayoga, S.T."
    assert content["data"]["bride_full_name"] == "Aiko Sakura, S.E."
    assert content["_meta"]["template_id"] == str(NEW_ID)
    order = c.execute("SELECT status, template_id FROM orders WHERE id=?",
                      (order_id,)).fetchone()
    assert order is not None
    assert order[0] == "draft"                           # NOT marked paid
    assert order[1] == NEW_ID
    conn.close()


@pytest.mark.asyncio
async def test_repeated_save_updates_same_invitation(aiohttp_client):
    """TEST K + L: second save updates SAME invitation; no duplicate rows."""
    client = await aiohttp_client(create_app())
    _, j1 = await _save(client, VALID_DATA)
    updated = dict(VALID_DATA)
    updated["title"] = "Witan & Aiko II"
    _, j2 = await _save(client, updated)                 # same cookie session
    assert j2["invitation_id"] == j1["invitation_id"]
    n_inv, n_ord = _db_counts(client)
    assert n_inv == 1 and n_ord == 1
    import sqlite3
    conn = sqlite3.connect(config.DB_NAME)
    row = conn.execute("SELECT content_json FROM invitations WHERE id=?",
                       (j1["invitation_id"],)).fetchone()
    conn.close()
    assert "Witan &amp; Aiko II" in row[0] or "Witan & Aiko II" in row[0]


@pytest.mark.asyncio
async def test_draft_hijack_is_ignored(aiohttp_client):
    """draft_id echo only honoured when owned by this token+template."""
    client = await aiohttp_client(create_app())
    resp = await client.post("/editor/save",
                             json={"template_id": NEW_ID, "data": VALID_DATA,
                                   "draft_id": 99999})
    j = await resp.json()
    assert j["ok"]                                       # creates its own draft
    n_inv, n_ord = _db_counts(client)
    assert n_inv == 1 and n_ord == 1


@pytest.mark.asyncio
async def test_preview_reflects_current_data(aiohttp_client):
    """TEST M: preview renders the submitted editor data (mode=preview)."""
    client = await aiohttp_client(create_app())
    resp = await client.post("/editor/preview",
                             json={"template_id": NEW_ID, "data": VALID_DATA})
    assert resp.status == 200
    j = await resp.json()
    assert j["ok"] is True
    assert "Witan Prayoga" in j["html"]
    assert "Aiko Sakura" in j["html"]
    assert "Rian Pratama" not in j["html"]               # not stale preview.json
    # Preview mode decoration applied exactly once (no double watermark).
    assert j["html"].count("PREVIEW MODE") == 1


@pytest.mark.asyncio
async def test_preview_does_not_touch_db(aiohttp_client):
    """TEST N + O: preview performs zero invitation/order writes."""
    client = await aiohttp_client(create_app())
    before = _db_counts(client)
    resp = await client.post("/editor/preview",
                             json={"template_id": NEW_ID, "data": VALID_DATA})
    assert resp.status == 200
    assert (await resp.json())["ok"]
    # And again after a draft exists — still no writes.
    await _save(client, VALID_DATA)
    mid = _db_counts(client)
    resp = await client.post("/editor/preview",
                             json={"template_id": NEW_ID, "data": VALID_DATA})
    assert resp.status == 200
    assert _db_counts(client) == mid
    assert mid[0] == 1 and mid[1] == 1                   # save created exactly one pair
    assert before == (0, 0)


@pytest.mark.asyncio
async def test_preview_legacy_template_rejected(aiohttp_client):
    client = await aiohttp_client(create_app())
    resp = await client.post("/editor/preview",
                             json={"template_id": LEGACY_ID, "data": VALID_DATA})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_no_arbitrary_filesystem_path_control(aiohttp_client):
    """TEST P: endpoints accept ONLY numeric template ids -> resolver paths."""
    client = await aiohttp_client(create_app())
    for evil in ["../../etc", "../gold", "/etc/passwd", 0, -5, "1 OR 1=1"]:
        resp = await client.post("/editor/preview",
                                 json={"template_id": evil, "data": VALID_DATA})
        assert resp.status == 400                        # rejected at parse stage
        resp = await client.post("/editor/save",
                                 json={"template_id": evil, "data": VALID_DATA})
        assert resp.status == 400
    # Traversal-ish query on GET editor is resolved via DB lookup only.
    resp = await client.get("/editor?id=../../etc")
    assert resp.status == 404
    n_inv, n_ord = _db_counts(client)
    assert n_inv == 0 and n_ord == 0


@pytest.mark.asyncio
async def test_malformed_payload_rejected_safely(aiohttp_client):
    """TEST R: non-JSON / wrong-shape bodies get structured 400s."""
    client = await aiohttp_client(create_app())
    cases = [
        ("{not json at all", "application/json"),
        ('["array", "not", "object"]', "application/json"),
        ('{"data": {"title": "x"}}', "application/json"),           # no template_id
        ('{"template_id": "abc", "data": {}}', "application/json"), # non-int id
        ('{"template_id": 1, "data": "nope"}', "application/json"), # data not obj
    ]
    for body, ctype in cases:
        resp = await client.post("/editor/save", data=body,
                                 headers={"Content-Type": ctype})
        assert resp.status == 400, body
        j = await resp.json()
        assert j["ok"] is False and "error" in j
    n_inv, n_ord = _db_counts(client)
    assert n_inv == 0 and n_ord == 0


@pytest.mark.asyncio
async def test_jinja_injection_cannot_execute(aiohttp_client):
    """User data flows into Jinja VARIABLES only — never as template source."""
    client = await aiohttp_client(create_app())
    payload = dict(VALID_DATA)
    payload["title"] = "{{ 7*7 }}"
    resp = await client.post("/editor/preview",
                             json={"template_id": NEW_ID, "data": payload})
    j = await resp.json()
    assert j["ok"]
    assert "{{ 7*7 }}" in j["html"]                      # literal text, escaped
    assert "<p>49</p>" not in j["html"]                  # not evaluated


@pytest.mark.asyncio
async def test_transaction_rollback_leaves_no_orphan_order(aiohttp_client, monkeypatch):
    """If invitation insert fails after order insert, everything rolls back."""
    client = await aiohttp_client(create_app())

    import db as db_mod
    real_get_conn = db_mod.get_conn

    class _BoomConn:
        """Passthrough wrapper that explodes right after the order INSERT."""
        def __init__(self, inner):
            self._inner = inner
            self._inserted_order = False

        def cursor(self):
            outer = self

            class _Cur:
                def __init__(self):
                    self._c = outer._inner.cursor()

                def execute(self, sql, params=()):
                    if outer._inserted_order and "INSERT INTO invitations" in sql:
                        raise RuntimeError("simulated invitation failure")
                    rv = self._c.execute(sql, params)
                    if "INSERT INTO orders" in sql:
                        outer._inserted_order = True
                    return rv

                def __getattr__(self, name):
                    return getattr(self._c, name)

            return _Cur()

        def commit(self):
            self._inner.commit()

        def rollback(self):
            self._inner.rollback()

        def close(self):
            self._inner.close()

    def _boom_conn():
        return _BoomConn(real_get_conn())

    monkeypatch.setattr("routes.public.get_conn", _boom_conn)
    resp, j = await _save(client, VALID_DATA)
    assert resp.status == 500
    n_inv, n_ord = _db_counts(client)
    assert n_inv == 0 and n_ord == 0                     # no orphan order
