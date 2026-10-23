"""Load reviewed agent instructions from Markdown files."""

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).with_name("prompts")


@lru_cache(maxsize=None)
def get_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Missing agent prompt: {path}")
    return path.read_text(encoding="utf-8").strip()
