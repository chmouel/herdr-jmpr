"""User settings: ``config.toml`` next to ``names.tsv`` in the plugin config dir."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_FILE = "config.toml"

START_MODES = ("insert", "normal")

KNOWN_KEYS = {"start_mode"}


class SettingsError(Exception):
    """config.toml could not be parsed or holds an unknown value."""


@dataclass(frozen=True)
class Settings:
    # "insert" filters as you type; "normal" starts in the vim keymap.
    start_mode: str = "insert"


def settings_path() -> Path:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / CONFIG_FILE
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "herdr" / "plugins" / "config" / "herdr-zshz" / CONFIG_FILE


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Settings()
    except OSError as error:
        raise SettingsError(f"Could not read {path}: {error.strerror}") from error
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise SettingsError(f"Invalid {path}: {error}") from error
    unknown = sorted(set(data) - KNOWN_KEYS)
    if unknown:
        raise SettingsError(f"Invalid {path}: unknown key {unknown[0]!r}")
    start_mode = data.get("start_mode", Settings.start_mode)
    if start_mode not in START_MODES:
        allowed = ", ".join(repr(mode) for mode in START_MODES)
        raise SettingsError(f"Invalid {path}: start_mode must be one of {allowed}")
    return Settings(start_mode=start_mode)
