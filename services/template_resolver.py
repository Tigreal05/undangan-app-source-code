"""Template resolver (Phase 1 of the template-engine migration).

One place that answers: *"where does this DB template live and which source
should render it?"* — no raw HTML is ever returned by this module.

Resolution priority (never reversed):

    NEW TEMPLATE
        templates_html/<tier_code>/<template_code>/template.html
        (root = config.TEMPLATES_HTML_DIR; a relative ``source_dir`` from the
         DB row is tried first when set)
            ↓ if not available
    LEGACY fallback
        templates.html_code column (explicitly marked ``source="legacy"``)

The legacy fallback is temporary: once every template has been converted to
the data-driven layout, html_code will be dropped in a later phase.

Security: tier codes / template codes coming from the database are validated
against a strict slug pattern and the final path is re-checked to stay inside
``config.TEMPLATES_HTML_DIR`` — values like ``../../etc/passwd`` are rejected
(``InvalidTemplateSlug``), never joined blindly.
"""
import json
import os
import re

import config
from db import get_conn
from services import render as render_svc

#: Valid tier/code slugs: lowercase alphanumerics, dashes and underscores only.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

SOURCE_NEW = "new"
SOURCE_LEGACY = "legacy"

#: Columns fetched for every resolution (single query, used everywhere).
TEMPLATE_SELECT = (
    "SELECT t.id, t.name, t.code, t.tier_id, ti.code, t.source_dir,"
    " t.schema_json, t.preview_json, t.html_code"
    " FROM templates t LEFT JOIN tiers ti ON t.tier_id = ti.id"
    " WHERE {clause}"
)


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------

class TemplateResolutionError(Exception):
    """Base class for every resolver failure."""


class TemplateNotFound(TemplateResolutionError):
    """No template row exists in the database for the given id/code."""


class InvalidTemplateSlug(TemplateResolutionError):
    """Tier code or template code failed validation (path-traversal guard)."""


class TemplateSourceUnavailable(TemplateResolutionError):
    """DB row exists but neither the new source nor legacy html_code can
    render it (strict mode only — the default resolves to source='legacy')."""


class TemplateInvalid(TemplateResolutionError):
    """The template directory exists but its template.html is missing."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def validate_slug(value, kind="code"):
    """Return ``value`` lowercased when it is a safe filesystem slug."""
    text = str(value or "").strip().lower()
    if not text or ".." in text or not _SLUG_RE.match(text):
        raise InvalidTemplateSlug(f"invalid {kind}: {value!r}")
    return text


def _templates_root():
    """Absolute, normalised root for converted templates (never hardcoded)."""
    return os.path.realpath(config.TEMPLATES_HTML_DIR)


def _safe_join(*parts):
    """Join under the templates root, rejecting anything that escapes it."""
    root = _templates_root()
    candidate = os.path.realpath(os.path.join(root, *parts))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise InvalidTemplateSlug(
            f"path escapes TEMPLATES_HTML_DIR: {parts!r}")
    return candidate


def _row_to_dict(row):
    """Normalise a TEMPLATE_SELECT row into a plain dict.

    Accepts mappings (already column-named) as well as positional tuples
    following ``TEMPLATE_SELECT`` order.
    """
    if not isinstance(row, tuple):
        d = dict(row)
        # Positional cursor rows keep the raw SELECT labels (e.g. "ti.code");
        # map them onto the canonical names so callers never see both.
        if "tier_code" not in d and "ti.code" in d:
            d["tier_code"] = d.pop("ti.code")
        return {
            "id": d.get("id"),
            "name": d.get("name"),
            "code": d.get("code") or "",
            "tier_id": d.get("tier_id"),
            "tier_code": d.get("tier_code") or d.get("ti.code") or "",
            "source_dir": d.get("source_dir") or "",
            "schema_json": d.get("schema_json") or "{}",
            "preview_json": d.get("preview_json") or "{}",
            "html_code": d.get("html_code") or "",
        }
    (tid, name, code, tier_id, tier_code, source_dir,
     schema_json, preview_json, html_code) = row
    return {
        "id": tid,
        "name": name,
        "code": code or "",
        "tier_id": tier_id,
        "tier_code": tier_code or "",
        "source_dir": source_dir or "",
        "schema_json": schema_json or "{}",
        "preview_json": preview_json or "{}",
        "html_code": html_code or "",
    }


def _is_traversal_source(source_dir, tier_code, code):
    """True when the row's declared location itself attempts to escape the
    templates root (malicious DB data — always legacy, even without strict)."""
    suspicious = False
    if source_dir:
        parts = [p for p in str(source_dir).replace("\\", "/").split("/")
                 if p]
        if any(p == ".." for p in parts) or os.path.isabs(str(source_dir)):
            suspicious = True
    for value, kind in ((tier_code, "tier code"), (code, "template code")):
        try:
            validate_slug(value, kind)
        except InvalidTemplateSlug:
            suspicious = True
    return suspicious


def _candidate_dirs(tier_code, code, source_dir):
    """Ordered list of directories that may hold the new template source."""
    candidates = []
    # Explicit relative source_dir wins over the conventional layout.
    if source_dir:
        try:
            candidates.append(_safe_join(source_dir.replace("\\", "/")))
        except InvalidTemplateSlug:
            pass
    if tier_code and code:
        try:
            candidates.append(
                _safe_join(validate_slug(tier_code, "tier code"),
                           validate_slug(code, "template code")))
        except InvalidTemplateSlug:
            pass
    return candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_template(template_row, *, strict=False):
    """Resolve one DB template row (mapping, or tuple with the columns of
    ``TEMPLATE_SELECT`` in order) into a location descriptor.

    Returns a dict::

        {id, name, code, tier_id, tier_code, source_dir,
         template_dir, template_path, schema_path, preview_path,
         source, html_code}

    ``source`` is ``"new"`` when ``template.html`` exists at the resolved
    directory, otherwise ``"legacy"`` (html_code fallback). With
    ``strict=True`` an unconvertible template raises instead:
    ``TemplateInvalid`` when the directory exists without template.html,
    ``TemplateSourceUnavailable`` when there is no new source and no
    legacy html_code either.
    """
    row = _row_to_dict(template_row)
    tier_code, code, source_dir = row["tier_code"], row["code"], row["source_dir"]

    # Malicious / invalid location data never reaches the filesystem: such a
    # row can only ever render through the legacy html_code fallback.
    traversal = _is_traversal_source(source_dir, tier_code, code)

    chosen = None
    dir_exists = False
    if not traversal:
        for cand in _candidate_dirs(tier_code, code, source_dir):
            if os.path.isfile(os.path.join(cand, "template.html")):
                chosen = cand
                break
            if os.path.isdir(cand):
                dir_exists = True

    if chosen is not None:
        source = SOURCE_NEW
    elif traversal:
        source = SOURCE_LEGACY
        if strict and not row["html_code"]:
            raise InvalidTemplateSlug(
                f"template {row['id']} declares an unsafe source location "
                "and has no legacy html_code")
    elif strict and dir_exists:
        raise TemplateInvalid(
            f"template dir exists without template.html: {code!r}")
    else:
        source = SOURCE_LEGACY
        if strict and not row["html_code"]:
            raise TemplateSourceUnavailable(
                f"template {row['id']} has neither a new source nor html_code")

    out = dict(row)
    out["source"] = source
    out["template_dir"] = chosen or ""
    out["template_path"] = (os.path.join(chosen, "template.html")
                            if chosen else "")
    out["schema_path"] = (os.path.join(chosen, "schema.json")
                          if chosen else "")
    out["preview_path"] = (os.path.join(chosen, "preview.json")
                           if chosen else "")
    return out


def fetch_template_row(cursor=None, *, template_id=None, code=None,
                       tier_code=None):
    """Fetch the resolver column set for one template (by id, or code+tier).

    Uses the caller's cursor when given, otherwise opens/closes its own
    connection through ``db.get_conn``.
    """
    if template_id is None and code is None:
        raise ValueError("template_id or code is required")
    if cursor is not None:
        return _fetch_from_cursor(
            cursor, template_id=template_id, code=code, tier_code=tier_code)
    conn = get_conn()
    try:
        return _fetch_from_cursor(
            conn.cursor(), template_id=template_id,
            code=code, tier_code=tier_code)
    finally:
        conn.close()


def _fetch_from_cursor(cursor, *, template_id, code, tier_code):
    if template_id is not None:
        cursor.execute(TEMPLATE_SELECT.format(clause="t.id = ?"),
                       (template_id,))
    elif tier_code:
        cursor.execute(
            TEMPLATE_SELECT.format(clause="t.code = ? AND ti.code = ?"),
            (code, tier_code))
    else:
        cursor.execute(TEMPLATE_SELECT.format(clause="t.code = ?"), (code,))
    return cursor.fetchone()


def resolve_template_by_id(template_id, *, strict=False):
    """Resolve the template with the given DB id. Raises TemplateNotFound
    when no such row exists."""
    row = fetch_template_row(template_id=template_id)
    if row is None:
        raise TemplateNotFound(f"template id not found: {template_id!r}")
    return resolve_template(row, strict=strict)


def resolve_template_by_code(code, tier_code=None, *, strict=False):
    """Resolve the template with the given code (optionally within a tier).
    Raises InvalidTemplateSlug for unsafe inputs and TemplateNotFound when
    no such row exists."""
    validate_slug(code, "template code")
    if tier_code:
        validate_slug(tier_code, "tier code")
    row = fetch_template_row(code=code, tier_code=tier_code)
    if row is None:
        raise TemplateNotFound(f"template code not found: {code!r}")
    return resolve_template(row, strict=strict)


def load_preview_data(resolved):
    """Demo/sample data for a resolved template.

    Priority: ``preview.json`` next to template.html (via
    ``services.render.load_preview``) → DB ``preview_json`` column → {}.
    When the descriptor has no ``template_dir`` yet the row does declare a
    conventional location, the standard layout is probed lazily so callers
    that resolved BEFORE the files were converted still pick preview.json up.
    Routes must NOT hardcode demo values; this is the single source.
    """
    template_dir = resolved.get("template_dir") or ""
    if not template_dir:
        tier_code = resolved.get("tier_code")
        code = resolved.get("code")
        if tier_code and code:
            try:
                guess = _safe_join(validate_slug(tier_code, "tier code"),
                                   validate_slug(code, "template code"))
            except InvalidTemplateSlug:
                guess = ""
            if guess and os.path.isfile(os.path.join(guess, "preview.json")):
                template_dir = guess
    if template_dir:
        data = render_svc.load_preview(template_dir)
        if data:
            return data
    try:
        data = json.loads(resolved.get("preview_json") or "{}")
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}
