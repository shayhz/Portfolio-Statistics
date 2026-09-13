"""Configuration loaded from the local .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


@dataclass
class Settings:
    client_id: str
    client_secret: str
    refresh_token: str
    accounts: list[str]
    sandbox: bool

    def missing(self) -> list[str]:
        """Names of the required credentials that are not filled in."""
        required = {
            "TASTYTRADE_CLIENT_ID": self.client_id,
            "TASTYTRADE_CLIENT_SECRET": self.client_secret,
            "TASTYTRADE_REFRESH_TOKEN": self.refresh_token,
        }
        return [name for name, value in required.items() if not value]


def load_settings() -> Settings:
    load_dotenv(ENV_FILE, override=True)
    accounts = [
        a.strip()
        for a in (os.getenv("TASTYTRADE_ACCOUNT") or "").split(",")
        if a.strip()
    ]
    return Settings(
        client_id=os.getenv("TASTYTRADE_CLIENT_ID", "").strip(),
        client_secret=os.getenv("TASTYTRADE_CLIENT_SECRET", "").strip(),
        refresh_token=os.getenv("TASTYTRADE_REFRESH_TOKEN", "").strip(),
        accounts=accounts,
        sandbox=(os.getenv("TASTYTRADE_SANDBOX", "false").strip().lower()
                 in ("1", "true", "yes")),
    )
