"""Application configuration loaded from environment variables (.env supported)."""
import os

from dotenv import load_dotenv

load_dotenv()

DB_NAME = os.getenv("DB_PATH", "undangan.db")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./static_uploads")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
HOMEPAGE_FILE = "homepage.html"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "invite.sukamoto.web.id")

# Template engine directories (Phase 2)
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
#: Data-driven templates: templates_html/<tier>/<code-name>/{template.html,schema.json,preview.json}
TEMPLATES_HTML_DIR = os.getenv("TEMPLATES_HTML_DIR", os.path.join(REPO_ROOT, "templates_html"))
#: Original static HTML files (legacy / not-yet-converted templates)
LEGACY_TEMPLATES_DIR = os.getenv("LEGACY_TEMPLATES_DIR", os.path.join(REPO_ROOT, "templates"))

# Upload hardening policy
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "mp3", "mp4"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024        # 10 MB for images / audio
MAX_VIDEO_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB for videos

# Login rate limiting: MAX_ATTEMPTS failures per WINDOW seconds, per IP
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 10 * 60


def ensure_dirs():
    """Create runtime directories/files that the app expects."""
    if not os.path.exists(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR)

    if not os.path.exists(HOMEPAGE_FILE):
        default_home = """
        <div class="bio-card">
            <h4>Visual Storyteller | Portrait & Editorial</h4>
            <p>Menyediakan jasa pembuatan undangan digital interaktif dan profesional untuk momen spesial Anda.</p>
            <div class="quote">"Capturing your precious moments with cinematic digital storytelling."</div>
        </div>
        """
        with open(HOMEPAGE_FILE, "w", encoding="utf-8") as f:
            f.write(default_home)
