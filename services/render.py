"""Data-driven invitation renderer (Phase 2).

A converted template lives in ``templates_html/<tier>/<code-name>/`` and holds:

    template.html   Jinja2 markup fed with ``d`` (invitation data) and
                    ``theme`` (CSS-variable overrides). Feature-gated blocks
                    are wrapped in elements carrying ``data-feature="<key>"``.
    schema.json     Field definitions used to build the admin/client form.
    preview.json    Sample data built from the ORIGINAL hardcoded values, so
                    rendering it must look identical to the static file.

``render_invitation(template_dir, data, theme, tier_code, mode)`` renders the
template, strips every section whose feature is disabled for the tier (via
services.tiers.has_feature) and decorates the page for the requested mode:

    demo     → watermark + guest greeting from ?to= (public /demo page)
    preview  → iframe preview of the client's real draft
    live     → published invitation (POSTs to /api/i/{slug}/rsvp|wish)

In demo/preview mode the RSVP & wish forms post to a JS stub that shows an
"in-memory demo" message instead of hitting the API.
"""
import json
import os

import jinja2
from markupsafe import Markup

from services import tiers as tiers_svc

MODES = ("demo", "preview", "live")

_env = jinja2.Environment(
    autoescape=True,
    undefined=jinja2.StrictUndefined,
    trim_blocks=False,
    lstrip_blocks=False,
    keep_trailing_newline=True,
)


# ---------------------------------------------------------------------------
# Template directory helpers
# ---------------------------------------------------------------------------

def read_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_schema(template_dir):
    """Field schema of a converted template ({} when missing)."""
    return read_json(os.path.join(template_dir, "schema.json"))


def load_preview(template_dir):
    """Preview data seeded from the original hardcoded values."""
    return read_json(os.path.join(template_dir, "preview.json"))


# ---------------------------------------------------------------------------
# Theme → CSS variables
# ---------------------------------------------------------------------------

#: Canonical CSS custom properties every converted template must consume.
THEME_VAR_MAP = {
    "primary": "--c-primary",
    "accent": "--c-accent",
    "bg": "--c-bg",
    "text": "--c-text",
    "font_head": "--font-head",
    "font_body": "--font-body",
}

import re as _re

_FONT_RE = _re.compile(r"^[A-Za-z0-9 ,.'\"()%-]+$")
_COLOR_RE = _re.compile(r"^#[0-9A-Fa-f]{3,8}$")


def _safe_color(value):
    return value if _COLOR_RE.match(str(value or "")) else None


def _safe_font(value):
    value = str(value or "")
    return value if value and _FONT_RE.match(value) else None


def theme_vars(theme):
    """Return [(css_var, literal_value), ...] — validated, never raw HTML."""
    theme = theme or {}
    out = []
    for key, var in THEME_VAR_MAP.items():
        value = theme.get(key)
        if value is None:
            continue
        if key.startswith("font"):
            value = _safe_font(value)
        else:
            value = _safe_color(value)
        if value:
            out.append((var, value))
    return out


# ---------------------------------------------------------------------------
# Mode banners / API wiring
# ---------------------------------------------------------------------------

DEMO_BADGE_HTML = (
    '<div style="position:fixed; bottom:10px; right:10px; background:rgba(0,0,0,0.7);'
    ' color:#fbbf24; font-size:10px; padding:4px 8px; border-radius:4px; z-index:9999;">'
    'SUKA MOTO DEMO MODE</div>'
)

PROTECT_SCRIPT_HTML = """
    <script>
        document.addEventListener('contextmenu', event => event.preventDefault());
        document.onkeydown = function(e) {
            if(e.keyCode == 123 || (e.ctrlKey && e.shiftKey && (e.keyCode == 73 || e.keyCode == 74)) || (e.ctrlKey && e.keyCode == 85)) {
                return false;
            }
        };
    </script>
"""

#: Client-side stub used in demo/preview mode: no backend, in-memory only.
DEMO_FORM_SCRIPT_HTML = """
    <script>
    (function () {
        var DEMO_MSG = "Mode demo: data hanya disimpan di memori browser dan tidak terkirim ke server.";
        function bindDemoForm(form, endpoint) {
            if (!form || form.dataset.demoBound === "1") return;
            form.dataset.demoBound = "1";
            form.setAttribute("action", endpoint);
            form.setAttribute("method", "POST");
            form.addEventListener("submit", function (ev) {
                ev.preventDefault();
                try {
                    var key = "__sukamoto_demo_" + endpoint;
                    var payload = new FormData(form);
                    var entry = {};
                    payload.forEach(function (v, k) { entry[k] = String(v); });
                    var list = JSON.parse(window.localStorage.getItem(key) || "[]");
                    list.push(entry);
                    window.localStorage.setItem(key, JSON.stringify(list));
                } catch (e) { /* private mode etc. — ignore */ }
                if (typeof window.showToast === "function") {
                    window.showToast(DEMO_MSG);
                } else {
                    window.alert(DEMO_MSG);
                }
            });
        }
        document.querySelectorAll("form[data-api]").forEach(function (form) {
            bindDemoForm(form, form.getAttribute("data-api"));
        });
    })();
    </script>
"""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TemplateNotFound(Exception):
    pass


def render_invitation(template_dir, data=None, theme=None, tier_code="silver",
                      mode="demo", slug=None, guest_name=None):
    """Render a converted template.

    :param template_dir: directory containing template.html (+ schema/preview)
    :param data: invitation dict ``d``; falls back to preview.json
    :param theme: dict of CSS variable overrides (see THEME_VAR_MAP)
    :param tier_code: silver|gold|platinum — drives feature stripping
    :param mode: "demo" | "preview" | "live"
    :param slug: invitation slug (live mode API endpoints)
    :param guest_name: personal greeting from ?to=
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")

    tpl_path = os.path.join(template_dir, "template.html")
    if not os.path.isdir(template_dir) or not os.path.exists(tpl_path):
        raise TemplateNotFound(template_dir)

    if data is None:
        data = load_preview(template_dir)

    api_slug = slug or (data.get("slug") if isinstance(data, dict) else None) or "demo"

    with open(tpl_path, "r", encoding="utf-8") as f:
        source = f.read()

    html = _env.from_string(source).render(
        d=data,
        theme=theme_vars(theme),
        mode=mode,
        slug=api_slug,
        guest_name=guest_name,
    )

    html = strip_disabled_features(html, tier_code)
    html = wire_forms(html, api_slug, mode)
    html = decorate_mode(html, mode)
    return html


def strip_disabled_features(html: str, tier_code: str) -> str:
    """Remove every element whose data-feature is not enabled for the tier.

    Balanced-tag removal (handles nested divs/sections); unknown feature keys
    are treated as enabled so nothing silently disappears on typos.
    """
    for match in _re.finditer(r'data-feature="([^"]+)"', html):
        key = match.group(1)
        if tiers_svc.has_feature(tier_code, key):
            continue
        start = html.rfind("<", 0, match.start())          # opening tag of the element
        if start == -1:
            continue
        end = _find_element_end(html, start, key)
        if end is None:
            continue
        html = html[:start] + html[end:]
    return html


def _find_element_end(html: str, tag_start: int, key: str):
    """Index just after the closing tag that matches the element opened at
    ``tag_start``; returns None when unbalanced."""
    m = _re.match(r"<([a-zA-Z][a-zA-Z0-9]*)", html[tag_start:])
    if not m:
        return None
    tag = m.group(1)
    pos = html.find(">", tag_start)
    if pos == -1:
        return None
    pos += 1
    depth = 1
    pattern = _re.compile(
        rf"<(/?)({_re.escape(tag)})(?=[\s/>])|<!--", _re.IGNORECASE)
    while depth:
        nxt = pattern.search(html, pos)
        if nxt is None:
            return None
        if nxt.group(0) == "<!--":
            close = html.find("-->", nxt.end())
            if close == -1:
                return None
            pos = close + 3
            continue
        depth += -1 if nxt.group(1) == "/" else 1
        pos = nxt.end()
    close = html.find(">", pos)
    if close == -1:
        return None
    return close + 1


def wire_forms(html: str, slug: str, mode: str) -> str:
    """Point data-api forms at /api/i/{slug}/… (live) or leave them bound to
    the in-memory demo stub (demo/preview)."""
    def _rewrite(m):
        full = m.group(0)
        endpoint = m.group(1)
        kind = "rsvp" if "rsvp" in endpoint else "wish"
        real = f"/api/i/{slug}/{kind}"
        if mode == "live":
            return full.replace(f'data-api="{endpoint}"', f'data-api="{real}"')
        return full
    return _re.sub(r'<form[^>]*data-api="([^"]*)"[^>]*>', _rewrite, html)


def decorate_mode(html: str, mode: str) -> str:
    """Insert mode-specific badge/scripts right after <body>."""
    inject = ""
    if mode == "demo":
        inject = PROTECT_SCRIPT_HTML + DEMO_BADGE_HTML + DEMO_FORM_SCRIPT_HTML
    elif mode == "preview":
        inject = DEMO_FORM_SCRIPT_HTML
    if not inject:
        return html
    body_at = _re.search(r"<body[^>]*>", html, _re.IGNORECASE)
    if not body_at:
        return html + inject
    return html[:body_at.end()] + inject + html[body_at.end():]


# Legacy alias kept for convenience in templates/tests.
as_markup = Markup
