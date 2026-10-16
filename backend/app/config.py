from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)


def database_path() -> Path:
    value = os.getenv("FINAGENT_DB_PATH")
    return Path(value).expanduser().resolve() if value else ROOT / "var" / "finagent.sqlite3"


def is_demo_admin_request(host: str | None) -> bool:
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}
