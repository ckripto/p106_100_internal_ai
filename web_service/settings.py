"""Web-service-specific configuration."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WebSettings:
    database_path: Path
    static_path: Path
    host: str
    port: int
    http_threads: int
    channel_timeout: int
    max_request_body_size: int
    queue_poll_interval: float


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent

SETTINGS = WebSettings(
    database_path=Path(os.environ.get("AGENTS_DB", PROJECT_ROOT / "data" / "agents.sqlite3")),
    static_path=PACKAGE_ROOT / "static",
    host=os.environ.get("WEB_HOST", "localhost"),
    port=int(os.environ.get("PORT", "80")),
    http_threads=int(os.environ.get("WEB_THREADS", "4")),
    channel_timeout=int(os.environ.get("WEB_CHANNEL_TIMEOUT", "30")),
    max_request_body_size=int(os.environ.get("WEB_MAX_REQUEST_BODY_SIZE", "16384")),
    queue_poll_interval=float(os.environ.get("WEB_QUEUE_POLL_INTERVAL", "1")),
)
