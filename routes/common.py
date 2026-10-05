"""Shared Jinja2 environment (autoescape ON) and common template fragments."""
import jinja2

_env = jinja2.Environment(
    autoescape=True,
    undefined=jinja2.StrictUndefined,
    trim_blocks=False,
    lstrip_blocks=False,
)


def render(template_str: str, **context) -> str:
    return _env.from_string(template_str).render(**context)


BASE_HEAD = """
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SUKA MOTO | Visual Storyteller</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Alex+Brush&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
        body { background: #09090b; color: #f4f4f5; display: flex; justify-content: center; }
        .container { width: 100%; max-width: 480px; min-height: 100vh; background: #0c0c0e; padding: 20px; border-left: 1px solid #27272a; border-right: 1px solid #27272a; position: relative; display: flex; flex-direction: column; justify-content: space-between; }
        .content-wrap { flex: 1; }
        .navbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; padding-bottom: 12px; border-bottom: 1px solid #27272a; }
        .brand-group { display: flex; flex-direction: column; text-decoration: none; }
        .brand-main { font-size: 15px; font-weight: 800; color: #fff; letter-spacing: 1px; }
        .brand-sub { font-family: 'Alex Brush', cursive; font-size: 22px; color: #fbbf24; margin-top: -8px; line-height: 1; }
        .nav-actions { display: flex; align-items: center; gap: 8px; }
        .hamburger-btn { background: none; border: none; color: #a1a1aa; font-size: 16px; cursor: pointer; padding: 6px; border-radius: 6px; transition: 0.2s; }
        .hamburger-btn:hover { color: #fbbf24; background: #18181b; }
        .menu-dropdown { display: none; position: absolute; top: 55px; right: 20px; background: #18181b; border: 1px solid #27272a; border-radius: 8px; width: 180px; z-index: 100; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5); }
        .menu-dropdown a { display: flex; align-items: center; gap: 8px; padding: 10px 12px; color: #f4f4f5; text-decoration: none; font-size: 11px; border-bottom: 1px solid #27272a; }
        .menu-dropdown a:last-child { border-bottom: none; }
        .menu-dropdown a:hover { background: #27272a; color: #fbbf24; border-radius: 8px; }
        .section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; color: #fbbf24; margin-bottom: 15px; font-weight: bold; }
        .footer { margin-top: 40px; border-top: 1px solid #27272a; padding: 25px 0 15px 0; text-align: center; }
        .footer-brand { font-size: 13px; font-weight: bold; color: #fff; margin-bottom: 6px; }
        .admin-footer-btn { background: none; border: none; color: #71717a; cursor: pointer; font-size: 12px; padding: 2px 6px; border-radius: 4px; transition: 0.2s; margin-left: 6px; }
        .admin-footer-btn:hover { color: #fbbf24; background: #18181b; }
        .footer-tagline { font-size: 11px; color: #a1a1aa; margin-bottom: 15px; }
        .social-icons { display: flex; justify-content: center; gap: 15px; margin-bottom: 15px; }
        .social-icons a { background: #18181b; border: 1px solid #27272a; width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fbbf24; font-size: 13px; text-decoration: none; transition: background 0.2s; }
        .social-icons a:hover { background: #fbbf24; color: #000; }
        .footer-web { font-size: 11px; color: #34d399; text-decoration: none; font-weight: 600; display: inline-block; margin-bottom: 15px; }
        .copyright { font-size: 10px; color: #71717a; border-top: 1px solid #18181b; padding-top: 12px; display: flex; align-items: center; justify-content: center; gap: 4px; }
    </style>
"""

FOOTER_HTML = """
    <div class="footer">
        <div class="footer-brand">
            SUKA MOTO
        </div>
        <div class="footer-tagline">Visual storyteller | Portrait & editorial</div>
        <div class="social-icons">
            <a href="https://instagram.com/sukaamotoo" target="_blank"><i class="fa-brands fa-instagram"></i></a>
            <a href="https://facebook.com" target="_blank"><i class="fa-brands fa-facebook-f"></i></a>
            <a href="https://wa.me/6285156918852" target="_blank"><i class="fa-brands fa-whatsapp"></i></a>
        </div>
        <a href="https://www.sukamoto.web.id" target="_blank" class="footer-web"><i class="fa-solid fa-globe"></i> www.sukamoto.web.id</a>
        <div class="copyright">
            &copy; 2026 SUKA MOTO. All Rights Reserved.
            <a class="admin-footer-btn" href="/admin" title="Admin Control"><i class="fa-solid fa-shield-halved"></i></a>
        </div>
    </div>
"""

NAV_SCRIPT = """
    <script>
        function toggleMenu() {
            var menu = document.getElementById("menuDropdown");
            menu.style.display = menu.style.display === "block" ? "none" : "block";
        }
        window.onclick = function(event) {
            if (!event.target.matches('.hamburger-btn') && !event.target.closest('.hamburger-btn')) {
                var menu = document.getElementById("menuDropdown");
                if (menu) menu.style.display = "none";
            }
        }
    </script>
"""
