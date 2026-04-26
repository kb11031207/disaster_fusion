"""Loaders for the project's two YAML config files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Anchor to the project root so cwd doesn't matter.
# src/shared/config.py -> shared -> src -> <project_root>
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"

DISASTER_TYPES_PATH = _CONFIG_DIR / "disaster_types.yaml"
THRESHOLDS_PATH = _CONFIG_DIR / "thresholds.yaml"


def load_disaster_config(disaster_type: str) -> dict[str, Any]:
    """
    Return the config block for one disaster type
    (pegasus_focus, severity_mapping, typical_damage_types).

    Raises ValueError if the type isn't defined in disaster_types.yaml.
    """
    with DISASTER_TYPES_PATH.open("r", encoding="utf-8") as fp:
        all_configs = yaml.safe_load(fp) or {}

    if disaster_type not in all_configs:
        known = ", ".join(sorted(all_configs.keys())) or "(none)"
        raise ValueError(
            f"Unknown disaster_type {disaster_type!r}. Known: {known}."
        )

    return all_configs[disaster_type]


def load_thresholds() -> dict[str, Any]:
    """Return the full thresholds.yaml as a dict."""
    with THRESHOLDS_PATH.open("r", encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}
