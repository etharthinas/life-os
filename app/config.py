"""Settings from environment variables, with a .env file in the project root as fallback."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:54329/lifeos")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Seoul"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Only this chat may use the bot. Leave empty at first: the bot replies with your chat id.
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# OAuth "Desktop app" client from Google Cloud (Google Auth Platform → Clients).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# Object storage for images (Cloudflare R2 or any S3 API). Without S3_BUCKET, files go to MEDIA_DIR.
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")  # R2: https://<account id>.r2.cloudflarestorage.com
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "auto")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", ROOT / ".dev" / "media"))
MEDIA_CACHE_DIR = Path(os.environ.get("MEDIA_CACHE_DIR", "/tmp/lifeos-media-cache"))
# Face detection/recognition models (OpenCV YuNet + SFace); the Docker image downloads them.
FACE_MODEL_DIR = Path(os.environ.get("FACE_MODEL_DIR", ROOT / ".dev" / "models"))
