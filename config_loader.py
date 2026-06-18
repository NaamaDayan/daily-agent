"""Loads and validates config.yaml, expands paths, caches the result."""

import os
import pathlib
from functools import lru_cache
from typing import Any

import yaml

_CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"


def _expand_paths(cfg: dict) -> dict:
    path_keys = {
        "typing_log_dir",
        "plans_dir",
        "learning_dir",
        "agent_log_dir",
        "pending_dir",
        "summarization_prompts_dir",
    }
    for key in path_keys:
        if key in cfg and isinstance(cfg[key], str):
            cfg[key] = str(pathlib.Path(cfg[key]).expanduser())
    return cfg


def _check_placeholders(cfg: dict, path: str = "") -> None:
    for key, value in cfg.items():
        full_key = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            _check_placeholders(value, full_key)
        elif isinstance(value, str) and "REPLACE_ME" in value:
            raise ValueError(
                f"config.yaml still contains a placeholder at '{full_key}': {value!r}\n"
                "Fill in the real value before running the agent."
            )


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {_CONFIG_PATH}")
    with open(_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    _check_placeholders(cfg)
    _expand_paths(cfg)
    return cfg
