"""Public /demo route tests after the Phase 1 resolver migration.

* New templates render through services.render (template.html + preview.json),
  NOT from templates.html_code.
* Legacy templates keep their old html_code behaviour.
* ?to= guest name is forwarded to the renderer.
* Demo watermark/protection appear exactly once (decorate_mode, no doubling).
"""
import os

import pytest

from app import create_app

NEW_ID = 1      # s01-classic-matcha — converted template (templates_html/)
LEGACY_ID = 2   # classic-floral — html_code only


async def _get(client, url):
    resp = await client.get(url)
    body = await resp.text()
    return resp, body


@pytest.mark.asyncio
async def test_demo_new_template_uses_rendered_file(aiohttp_client):
    """TEST 5: HTML comes from template.html, not templates.html_code."""
    client = await aiohttp_client(create_app())
    resp, body = await _get(client, f"/demo?id={NEW_ID}")
    assert resp.status == 200
    # Unique marker that exists ONLY in templates_html/.../template.html.
    assert "guest-greeting" in body or "The Wedding of" in body
    assert "--c-primary" in body                      # theme CSS vars from renderer
    # The legacy SAMPLE_HTML marker must be gone for new templates.
    assert 'class="editable"' not in body


@pytest.mark.asyncio
async def test_demo_legacy_template_still_works(aiohttp_client):
    """TEST 6: unconverted templates keep the legacy demo behaviour."""
    client = await aiohttp_client(create_app())
    resp, body = await _get(client, f"/demo?id={LEGACY_ID}")
    assert resp.status == 200
    assert "Rian & Siska" in body                     # SAMPLE_HTML content
    assert "SUKA MOTO DEMO MODE" in body              # legacy watermark injected
    assert "contextmenu" in body                      # legacy protection script


@pytest.mark.asyncio
async def test_demo_guest_name_forwarded(aiohttp_client):
    """TEST 7: ?to=Budi reaches the new renderer as guest_name."""
    client = await aiohttp_client(create_app())
    resp, body = await _get(client, f"/demo?id={NEW_ID}&to=Budi")
    assert resp.status == 200
    assert "Budi" in body
    resp_plain, body_plain = await _get(client, f"/demo?id={NEW_ID}")
    assert "Budi" not in body_plain


@pytest.mark.asyncio
async def test_demo_no_double_decoration(aiohttp_client):
    """TEST 8: watermark badge and protection script appear exactly once."""
    client = await aiohttp_client(create_app())
    resp, body = await _get(client, f"/demo?id={NEW_ID}&to=Budi")
    assert resp.status == 200
    assert body.count("SUKA MOTO DEMO MODE") == 1
    assert body.count("addEventListener('contextmenu'") == 1


@pytest.mark.asyncio
async def test_demo_unknown_id_is_404(aiohttp_client):
    client = await aiohttp_client(create_app())
    resp, _ = await _get(client, "/demo?id=99999")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_demo_preview_data_not_hardcoded_in_route(aiohttp_client):
    """Demo values come from preview.json; changing it changes the output."""
    import config
    tpl_dir = os.path.join(config.TEMPLATES_HTML_DIR, "silver",
                           "s01-classic-matcha")
    path = os.path.join(tpl_dir, "preview.json")
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()
    try:
        mutated = original.replace("Rian & Siska", "Test Pair XYZ")
        with open(path, "w", encoding="utf-8") as f:
            f.write(mutated)
        client = await aiohttp_client(create_app())
        resp, body = await _get(client, f"/demo?id={NEW_ID}")
        assert resp.status == 200
        assert "Test Pair XYZ" in body
        assert "Rian & Siska" not in body
    finally:
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)
