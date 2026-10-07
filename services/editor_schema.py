"""Schema-driven editor support (Phase 2).

Turns a converted template's ``schema.json`` into:

* form metadata for the editor page (``build_form_meta``), and
* validated invitation data + theme from browser JSON (``validate_editor_data``)

The schema file is the single source of truth. Data produced here mirrors the
shape of ``preview.json`` — a flat dict of field keys plus a nested ``theme``
dict — which is exactly what ``services/render.render_invitation`` consumes
(``d.<key>`` / ``theme``). Repeatable groups (events, gallery, accounts) are
supported as lists of item dicts.

Security notes:

* Keys not declared in the schema are ignored (no mass assignment) and
  values are coerced/checked per field type before anything is stored.
* Values are plain strings/ints/lists — never HTML sources; the renderer
  autoescapes every Jinja variable, so user data can never become template
  code.
* Select values must be one of the schema's declared options; colors must be
  hex literals; dates/times must parse.
"""
import datetime
import json
import re

from services import render as render_svc

#: Max accepted editor payload size (bytes) — generous for text-only drafts.
MAX_PAYLOAD_BYTES = 256 * 1024

_HEX_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
_SLUGISH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _#.&'\-/+,()]*$")


class EditorValidationError(Exception):
    """Raised with a {field: message} mapping of structured problems."""

    def __init__(self, errors):
        super().__init__(str(errors))
        self.errors = errors


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def load_schema(template_dir):
    """Field schema of a converted template ({} when missing)."""
    return render_svc.load_schema(template_dir)


def iter_field_defs(schema):
    """Yield ``(path, field_def)`` for every editable field in the schema.

    Paths use dot notation mirroring the data shape: group fields are plain
    keys (``title``, ``intro`` …), repeatable items are ``events[].label``,
    theme entries are ``theme.primary`` / ``theme.font_head``.
    """
    for group in schema.get("groups", []) or []:
        for f in group.get("fields", []) or []:
            if isinstance(f, dict) and f.get("key"):
                yield f["key"], f
    for rep in schema.get("repeatables", []) or []:
        if not (isinstance(rep, dict) and rep.get("key")):
            continue
        for f in rep.get("item_fields", []) or []:
            if isinstance(f, dict) and f.get("key"):
                yield f"{rep['key']}[].{f['key']}", f
    theme = schema.get("theme") or {}
    for section in ("colors", "fonts"):
        for f in theme.get(section, []) or []:
            if isinstance(f, dict) and f.get("key"):
                yield f"theme.{f['key']}", f


def allowed_keys(schema):
    return {path for path, _ in iter_field_defs(schema)}


def tier_available(schema_entry, tier_code):
    """True when a schema entry applies to the given tier (min_tier)."""
    min_tier = str(schema_entry.get("min_tier") or "").lower()
    if not min_tier:
        return True
    order = {"silver": 1, "gold": 2, "platinum": 3}
    return order.get(min_tier, 1) <= order.get(str(tier_code or "silver").lower(), 1)


# ---------------------------------------------------------------------------
# Form metadata for the editor page
# ---------------------------------------------------------------------------

def _prefixed_group_fields(schema):
    """Map flat data key -> prefixed form name for groups whose field keys
    collide across groups (e.g. groom.full_name / bride.full_name).

    Single-occurrence keys keep their plain name so preview.json sample keys
    still prefill the matching control.
    """
    counts = {}
    groups_of = {}
    for group in schema.get("groups", []) or []:
        gkey = group.get("key") or ""
        for f in group.get("fields", []) or []:
            k = f.get("key") if isinstance(f, dict) else None
            if k:
                counts[k] = counts.get(k, 0) + 1
                groups_of.setdefault(k, []).append(gkey)
    # Shared keys are prefixed with their OWNING group (not the last one
    # seen while scanning), so groom.full_name / bride.full_name both map.
    return {k: f"{g}.{k}" for k, gs in groups_of.items() if counts[k] > 1
            for g in [gs[0]]}


def build_form_meta(schema, tier_code="silver"):
    """Compact JSON structure driving the client-side form generator."""
    prefixes = _prefixed_group_fields(schema)
    groups = []
    for group in schema.get("groups", []) or []:
        fields = [_field_meta(f, prefixes) for f in (group.get("fields") or [])
                  if isinstance(f, dict) and f.get("key")]
        if fields:
            groups.append({"key": group.get("key", ""),
                           "label": group.get("label", ""),
                           "fields": fields})
    repeatables = []
    for rep in schema.get("repeatables", []) or []:
        if not isinstance(rep, dict) or not rep.get("key"):
            continue
        items = [_field_meta(f) for f in (rep.get("item_fields") or [])
                 if isinstance(f, dict) and f.get("key")]
        repeatables.append({"key": rep["key"],
                            "label": rep.get("label", ""),
                            "min_tier": rep.get("min_tier", "silver"),
                            "available": tier_available(rep, tier_code),
                            "fields": items})
    theme = []
    src = schema.get("theme") or {}
    for section in ("colors", "fonts"):
        for f in src.get(section, []) or []:
            if isinstance(f, dict) and f.get("key"):
                meta = _field_meta(f)
                meta["name"] = f"theme.{f['key']}"
                meta["available"] = tier_available(f, tier_code)
                theme.append(meta)
    return {"groups": groups, "repeatables": repeatables, "theme": theme}


def _field_meta(f, prefixes=None):
    key = f["key"]
    name = (prefixes or {}).get(key, key)
    return {
        "key": key,
        "name": name,
        "label": f.get("label", key),
        "type": f.get("type", "text"),
        "required": bool(f.get("required")),
        "max_length": int(f.get("max_length") or 0),
        "options": list(f.get("options") or []),
        "presets": [p.get("value") for p in (f.get("presets") or [])
                    if isinstance(p, dict)],
        "accept": f.get("accept", ""),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def parse_and_validate(payload, schema, tier_code="silver"):
    """Validate raw request JSON against the schema.

    Returns ``(data, theme)`` where ``data`` is the flat invitation dict
    (including repeatable lists) consumed as ``d`` by render_invitation and
    ``theme`` is the CSS-variable dict. Raises ``EditorValidationError`` with
    a {field: message} map on any problem.
    """
    errors = {}
    if payload is None or not isinstance(payload, dict):
        raise EditorValidationError({"_body": "Payload JSON object tidak valid."})

    field_index = {}
    item_index = {}          # repeater key -> {field: def}
    theme_index = {}
    group_of = {}            # flat field key -> owning group key
    groups_for = {}          # flat field key -> [owning group keys]
    shared_keys = set()      # keys declared in more than one group
    for group in schema.get("groups", []) or []:
        gkey = group.get("key") or ""
        for f in group.get("fields", []) or []:
            if isinstance(f, dict) and f.get("key"):
                k = f["key"]
                if k in group_of:
                    shared_keys.add(k)
                group_of[k] = gkey
                groups_for.setdefault(k, []).append(gkey)
    for path, fdef in iter_field_defs(schema):
        if path.startswith("theme."):
            theme_index[path.split(".", 1)[1]] = fdef
        elif "[]." in path:
            rep = path.split("[].", 1)[0]
            item_index.setdefault(rep, {})[path.split("[].", 1)[1]] = fdef
        else:
            field_index[path] = fdef

    data = {}
    theme = {}
    covered = set()          # fields supplied via a nested group object
    prefixes = _prefixed_group_fields(schema)   # flat key -> prefixed name

    def store(fk, gkey, raw):
        """Validate one scalar and store it under its canonical data key."""
        fdef = field_index[fk]
        out_key = f"{gkey}_{fk}" if fk in shared_keys else fk
        data[out_key] = _validate_scalar(f"{gkey}.{fk}" if gkey else fk,
                                         raw, fdef, errors)

    for key, value in payload.items():
        if key == "theme":
            theme = _validate_theme(value, theme_index, errors)
            continue
        if key in item_index:
            data[key] = _validate_repeater(key, value, item_index[key], errors)
            continue
        if isinstance(value, dict) and any(
                (g.get("key") == key for g in (schema.get("groups") or [])
                 if isinstance(g, dict))):
            # Nested group object: {"groom": {"full_name": ...}} — only the
            # keys it declares are taken from it; unknown inner keys dropped.
            for fk, raw in value.items():
                if fk not in field_index:
                    continue
                covered.add(fk)
                store(fk, key, raw)
            continue
        if key in field_index:
            data[key] = _validate_scalar(key, value, field_index[key], errors)
            covered.add(key)
            continue
        # Unknown keys are dropped (never persisted / rendered).

    # Prefixed aliases emitted by the generated form (e.g. groom.full_name).
    # Shared keys (groom/bride full_name) are stored under an underscored
    # flat alias (groom_full_name) so both values survive in one flat dict
    # AND remain resolvable by templates through the owning-group lookup.
    for key, value in payload.items():
        if "." not in key or key == "theme":
            continue
        gkey, _, fk = key.partition(".")
        if fk not in field_index or not isinstance(value, str):
            continue
        if gkey not in groups_for.get(fk, []):
            continue
        covered.add(fk)
        store(fk, gkey, value)

    # Canonicalise prefixed names back to their flat storage key for fields
    # that are NOT shared across groups (preview.json sample shape), so a
    # draft saved with e.g. {"couple.intro": "..."} still prefill/matches
    # the plain "intro" key on re-open and rendering.
    for pk, fk in ((v, k) for k, v in prefixes.items()):
        if pk in data and fk not in shared_keys:
            data[fk] = data.pop(pk)

    # Required scalars — checked against the RAW submitted value so an empty
    # string is never silently replaced by a template default at save time.
    for key, fdef in field_index.items():
        if not fdef.get("required"):
            continue
        # A field counts as covered only when a NON-EMPTY raw value was
        # supplied through one of its accepted names — an empty string must
        # never silence the required check.
        def _nonempty(v):
            return bool(str(v or "").strip())
        covered_ok = (key in covered) and any(
            _nonempty(payload.get(n)) for n in
            [key] + [f"{g}.{key}" for g in groups_for.get(key, [])])
        if covered_ok:
            continue
        gkeys = groups_for.get(key, [])
        supplied = _nonempty(payload.get(key))
        if not supplied:
            for gkey in gkeys:
                grp = payload.get(gkey)
                if isinstance(grp, dict) and str(grp.get(key, "") or "").strip():
                    supplied = True
                    break
                # Prefixed form name (groom.full_name) / stored alias
                # (groom_full_name) count as supplied too.
                if str(payload.get(f"{gkey}.{key}", "") or "").strip() or \
                        str(payload.get(f"{gkey}_{key}", "") or "").strip():
                    supplied = True
                    break
        if not supplied:
            # Shared keys (e.g. groom/bride full_name) are validated per
            # group above; the flat alias itself must not produce a second,
            # confusing error entry.
            if key in shared_keys:
                continue
            err_key = f"{gkeys[0]}.{key}" if key in shared_keys else key
            errors.setdefault(err_key, f"{fdef.get('label', key)} wajib diisi.")

    if errors:
        raise EditorValidationError(errors)
    return data, theme


def _validate_scalar(path, value, fdef, errors):
    ftype = fdef.get("type", "text")
    if value is None:
        value = ""
    if ftype == "image":
        # Image references stay URLs/paths for now (upload flow is a later
        # phase); accept only safe http(s)/relative paths, never markup.
        text = str(value)
        if len(text) > 500 or "<" in text or ">" in text:
            errors[path] = "Referensi gambar tidak valid."
            return ""
        if text and not (text.startswith("/") or text.startswith("http://")
                         or text.startswith("https://")
                         or re.match(r"^[A-Za-z0-9._\-/]+$", text)):
            errors[path] = "Referensi gambar tidak valid."
            return ""
        return text
    if not isinstance(value, str):
        errors[path] = "Format nilai tidak sesuai."
        return ""
    text = value.strip()
    max_len = int(fdef.get("max_length") or 0)
    if max_len and len(text) > max_len:
        errors[path] = f"Maksimal {max_len} karakter."
    if ftype == "select":
        options = list(fdef.get("options") or [])
        if text and options and text not in options:
            errors[path] = "Pilihan tidak tersedia."
    elif ftype == "color":
        if text and not _HEX_RE.match(text):
            errors[path] = "Warna harus format hex (contoh: #aabbcc)."
    elif ftype == "date":
        if text and not _valid_date(text):
            errors[path] = "Tanggal harus berformat YYYY-MM-DD."
    elif ftype == "time":
        if text and not _valid_time_any(text):
            errors[path] = "Format waktu tidak valid."
    elif ftype == "textarea":
        pass  # multiline free text; escaped by the renderer
    else:  # text
        if text and "\n" in text:
            text = text.replace("\n", " ")
    return text


def _valid_date(text):
    try:
        datetime.date.fromisoformat(text)
        return True
    except ValueError:
        return False


def _valid_time_any(text):
    if re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", text):
        return True
    return bool(_SLUGISH_RE.match(text)) and len(text) <= 40


def _validate_repeater(key, value, item_fields, errors):
    if not isinstance(value, list):
        errors[key] = "Data berulang harus berupa daftar."
        return []
    out = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            errors[f"{key}[{idx}]"] = "Item tidak valid."
            continue
        row = {}
        for fkey, fval in item.items():
            if fkey in item_fields:
                row[fkey] = _validate_scalar(
                    f"{key}[{idx}].{fkey}", fval, item_fields[fkey], errors)
            # unknown item keys are dropped, never persisted
        for fkey, fdef in item_fields.items():
            if fdef.get("required") and not str(row.get(fkey, "")).strip():
                errors[f"{key}[{idx}].{fkey}"] = \
                    f"{fdef.get('label', fkey)} wajib diisi."
        out.append(row)
    return out


def _validate_theme(value, theme_index, errors):
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors["theme"] = "Theme harus berupa objek."
        return {}
    theme = {}
    for key, val in value.items():
        fdef = theme_index.get(key)
        text = str(val or "").strip()
        if not text:
            continue
        # Canonical CSS variables (THEME_VAR_MAP) are always accepted by the
        # renderer's own validation — even when this template's schema does
        # not expose them as an editable option (e.g. preview.json sample
        # data such as theme.text). Unknown NON-css keys stay rejected.
        canonical = key in render_svc.THEME_VAR_MAP
        if fdef is None and not canonical:
            errors[f"theme.{key}"] = "Opsi tema tidak dikenal."
            continue
        if canonical:
            theme[key] = text
            continue
        ftype = fdef.get("type", "text")
        if ftype == "color" and not render_svc._safe_color(text):
            errors[f"theme.{key}"] = "Warna harus format hex."
        elif ftype == "select":
            options = list(fdef.get("options") or [])
            if options and text not in options:
                errors[f"theme.{key}"] = "Font tidak tersedia."
        else:
            safe = render_svc._safe_font(text)
            if not safe:
                errors[f"theme.{key}"] = "Nama font tidak valid."
            text = safe
        theme[key] = text
    return theme


# ---------------------------------------------------------------------------
# Draft <-> content_json serialisation
# ---------------------------------------------------------------------------

def draft_content(data, theme):
    """JSON string stored in invitations.content_json for a draft."""
    return json.dumps({"data": data, "theme": theme}, ensure_ascii=False)


def draft_data(content_json):
    """Inverse of :func:`draft_content`; returns (data, theme) or (None, {})."""
    try:
        blob = json.loads(content_json or "")
    except (ValueError, TypeError):
        return None, {}
    if isinstance(blob, dict) and isinstance(blob.get("data"), dict):
        return blob["data"], dict(blob.get("theme") or {})
    return None, {}
