"""Environment-driven configuration loaded once at app startup."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv(override=True)  # .env is authoritative in dev; container env wins in prod via Docker


class Config:
    def __init__(self) -> None:
        # Keys may be plain ("mykey") or scoped ("mykey:chat,admin").
        # ROUTER_API_KEYS stores plain key strings for fast O(1) lookup.
        # ROUTER_KEY_SCOPES maps key → frozenset of scopes.
        raw_keys = os.getenv("ROUTER_API_KEYS", "")
        self.ROUTER_API_KEYS: set[str] = set()
        self.ROUTER_KEY_SCOPES: dict[str, frozenset[str]] = {}
        for entry in raw_keys.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                key, scopes_str = entry.split(":", 1)
                key = key.strip()
                scopes = frozenset(s.strip() for s in scopes_str.split(",") if s.strip())
            else:
                key = entry
                scopes = frozenset()  # no scope restrictions — all access
            self.ROUTER_API_KEYS.add(key)
            self.ROUTER_KEY_SCOPES[key] = scopes

        self.DO_INFERENCE_BASE_URL: str = os.getenv(
            "DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"
        )
        # Never logged, never echoed in responses
        self.DO_INFERENCE_API_KEY: str = os.getenv("DO_INFERENCE_API_KEY", "")
        self.DO_DEFAULT_MODEL: str = os.getenv(
            "DO_DEFAULT_MODEL", "llama3.3-70b-instruct"
        )
        self.MAX_BODY_BYTES: int = int(os.getenv("MAX_BODY_BYTES", str(256 * 1024)))
        self.APP_ENV: str = os.getenv("APP_ENV", "development")
        self.SERVICE_VERSION: str = os.getenv("SERVICE_VERSION", "0.1.0")
