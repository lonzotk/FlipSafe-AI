"""Application defaults and the single secret loaded from the local .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
load_dotenv(ROOT.parent / ".env", override=False)

API_PROVIDER = "portkey"
PORTKEY_BASE_URL = "https://api.portkey.ai/v1"
PORTKEY_MODEL = "gpt-6-luna"
FRONTEND_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]

# Keep operational limits in code so PORTKEY_API_KEY is the only .env value.
MAX_REQUESTS_PER_MINUTE = 30
MAX_REQUESTS_PER_AGENT = 6
MAX_TOKENS_PER_CALL = 4096
MAX_TOKENS_PER_RUN = 12000
MAX_SPEND_PER_RUN_USD = 1.0
MAX_SPEND_PER_MONTH_USD = 25.0
MAX_TEXT_LENGTH = 6000
