"""Load the explicitly selected dotenv without shell evaluation or secret logging."""

import os
from pathlib import Path

from dotenv import load_dotenv


def load_environment(path: Path | None = None) -> None:
    selected = path or Path(os.environ.get("FIX_ENV_FILE", ".env"))
    load_dotenv(selected, override=False, interpolate=False)


def openai_api_key() -> str:
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API", "")
