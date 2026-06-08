"""Auto-load environment variables from project config.env.

Called at startup by all agent-journal entry points so agents never need
to manually ``source config.env && export GEMINI_API_KEY``.
"""

from __future__ import annotations

import os
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config.env"


def load_agent_env(config_path: Path | None = None) -> None:
    """Parse config.env and inject missing keys into os.environ.

    Handles bash-style ``${VAR:-default}`` values: uses the current env
    value if set, otherwise falls back to the default after ``:-``.
    Only sets keys that are not already in the environment (setdefault).
    """
    path = config_path or _CONFIG_PATH
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export").strip()
        val = val.strip().strip('"').strip("'")
        if val.startswith("${") and ":-" in val:
            default = val.split(":-", 1)[1].rstrip("}")
            val = os.environ.get(key, default)
        if val:
            os.environ.setdefault(key, val)
