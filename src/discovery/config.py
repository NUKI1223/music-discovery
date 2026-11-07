"""Explicit project dotenv loading; process configuration takes precedence."""

import os
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = "data/unified/community-genres-v4/catalog.json"
KEYS = frozenset({
    "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_INPUT_USD_PER_MILLION",
    "OPENROUTER_OUTPUT_USD_PER_MILLION", "MAX_COST_PER_REQUEST_USD", "MAX_COST_PER_DAY_USD", "MAX_INPUT_TOKENS", "MAX_INPUT_BYTES",
    "DISCOVERY_DB", "DISCOVERY_CATALOG", "DISCOVERY_MODEL_PATH",
})


def load_project_env(path: Path | None = None) -> None:
    """Read only the chosen project file, with no shell execution or secret output."""
    if os.environ.get("DISCOVERY_DOTENV") == "0":
        return
    path = path if path is not None else PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    values = dotenv_values(path, interpolate=False)
    for key in KEYS:
        value = values.get(key)
        if value is not None:
            os.environ.setdefault(key, value)
