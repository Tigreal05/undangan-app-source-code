"""TEMPORARY debug repro — will be deleted."""
from aiohttp import FormData
from app import create_app

VALID_DATA = {"title": "Witan & Aiko", "groom.full_name": "Witan Prayoga, S.T.",
              "bride.full_name": "Aiko Sakura, S.E.", "date_iso": "2026-12-12", "time_range": "09.00 WIB"}
CONFIRM_FORM = {"buyer_name": "Budi", "whatsapp": "08123456789", "bank": "BCA"}
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00"*64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00"*64
WEBP_BYTES = b"RIFF" + b"\x2c\x00\x00\x00" + b"WEBPVP8 " + b"\x00"*32


async def test_probe(aiohttp_client):
    client = await aiohttp_client(create_app())
    resp = await client.post("/editor/save", json={"template_id": 1, "data": VALID_DATA})
    body = await resp.json(); assert body["ok"], body
    r = await client.post("/checkout/confirm", data={"id": "1", **CONFIRM_FORM}, allow_redirects=False)
    code = r.headers["Location"].split("order=")[1]
    for i in range(4):
        form = FormData()
        form.add_field("order", code)
        form.add_field("file", JPG_BYTES, filename="bukti.jpg")
        rr = await client.post("/payment/proof", data=form, allow_redirects=False)
        print(f"PROBE {i}: status={rr.status} body={(await rr.text())[:120]!r}")
