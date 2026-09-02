#!/usr/bin/env python3

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import tomllib


APP_NAME = "syncatuna"


DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8765,

        "max_msg_size": 8192,
        "watchdog_interval": 2.0,

        "auto_advance_grace": 3.0,
        "manual_advance_grace": 3.0,

        "max_queue_size": 100,
        "max_url_length": 2048,
        "max_title_length": 200,
        "max_name_length": 24,
        "max_duration_seconds": 21600,

        "add_cooldown_seconds": 2.0,
    },

    "client": {
        "clock_sync_interval": 20.0,
        "ticker_interval": 1.0,
    },
}


def get_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")

    if base:
        return Path(base) / APP_NAME

    return Path.home() / ".config" / APP_NAME


def get_config_path() -> Path:
    return get_config_dir() / "config.toml"


def deep_merge(
    defaults: dict[str, Any],
    user: dict[str, Any],
) -> dict[str, Any]:
    result = deepcopy(defaults)

    for key, user_value in user.items():
        default_value = result.get(key)

        if (
            isinstance(default_value, dict)
            and isinstance(user_value, dict)
        ):
            result[key] = deep_merge(default_value, user_value)
        else:
            result[key] = user_value

    return result


def load_config() -> dict[str, Any]:
    path = get_config_path()

    if not path.exists():
        return deepcopy(DEFAULT_CONFIG)

    try:
        with path.open("rb") as file:
            user_config = tomllib.load(file)
    except OSError as exc:
        raise RuntimeError(
            f"Could not read configuration file {path}: {exc}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"Invalid TOML configuration in {path}: {exc}"
        ) from exc

    if not isinstance(user_config, dict):
        raise RuntimeError(
            f"Invalid configuration format in {path}."
        )

    return deep_merge(DEFAULT_CONFIG, user_config)