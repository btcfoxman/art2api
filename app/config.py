from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("ART_DATA_DIR", "data")))
    api_key: str = field(default_factory=lambda: os.getenv("ART_API_KEY", ""))
    admin_token: str = field(default_factory=lambda: os.getenv("ART_ADMIN_TOKEN", ""))
    encryption_key: str = field(default_factory=lambda: os.getenv("ART_ENCRYPTION_KEY", ""))
    public_base_url: str = field(default_factory=lambda: os.getenv("ART_PUBLIC_BASE_URL", "http://localhost:8797").rstrip("/"))
    mcp_url: str = "https://mcp.artlist.io/mcp"
    request_timeout: int = field(default_factory=lambda: int(os.getenv("ART_REQUEST_TIMEOUT_SECONDS", "60")))
    poll_interval: int = field(default_factory=lambda: int(os.getenv("ART_POLL_INTERVAL_SECONDS", "10")))
    task_timeout: int = field(default_factory=lambda: int(os.getenv("ART_TASK_TIMEOUT_SECONDS", "3600")))
    queue_limit: int = field(default_factory=lambda: int(os.getenv("ART_QUEUE_LIMIT", "100")))
    chrome_executable: str = field(default_factory=lambda: os.getenv("ART_CHROME_EXECUTABLE", ""))
    browser_headless: bool = field(default_factory=lambda: os.getenv('ART_BROWSER_HEADLESS', '0').lower() in {'1','true','yes'})
    browser_timeout: int = 900
    sd25_video_policy: str = 'adjust'
    oauth_client_id: str = field(default_factory=lambda: os.getenv("ART_OAUTH_CLIENT_ID", ""))
    version: str = "0.3.14"

    def validate(self):
        if len(self.api_key) < 24 or len(self.admin_token) < 24:
            raise ValueError("ART_API_KEY and ART_ADMIN_TOKEN must each contain at least 24 characters")
        if self.api_key == self.admin_token:
            raise ValueError("ART_API_KEY and ART_ADMIN_TOKEN must be different")
        if not self.encryption_key:
            raise ValueError("ART_ENCRYPTION_KEY is required; generate a Fernet key")
        if not self.public_base_url.startswith("https://") and not self.public_base_url.startswith(("http://localhost:", "http://127.0.0.1:")):
            raise ValueError("ART_PUBLIC_BASE_URL must use HTTPS or localhost")
        if min(self.poll_interval, self.request_timeout, self.task_timeout) <= 0 or self.queue_limit < 1:
            raise ValueError("timeouts, interval and queue limit must be positive")
