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

BACKENDS = ("textual", "auto", "fzf", "skim", "gum")
START_MODES = ("insert", "normal")

KNOWN_KEYS = {"backend", "start_mode", "theme"}


class SettingsError(Exception):
    """config.toml could not be parsed or holds an unknown value."""


@dataclass(frozen=True)
class Settings:
    # Picker frontend: "textual" by default; "auto" tries fzf -> skim -> gum -> textual.
    backend: str = "textual"
    # "insert" filters as you type; "normal" starts in the vim keymap (textual).
    start_mode: str = "insert"
    # Textual colour theme: "auto" syncs with Herdr's config.toml or uses "herdr-ink".
    theme: str = "auto"


def settings_path() -> Path:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / CONFIG_FILE
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "herdr" / "plugins" / "config" / "herdr-zshz" / CONFIG_FILE


def herdr_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "herdr" / CONFIG_FILE


def herdr_theme_name(path: Path | None = None) -> str | None:
    """Read ``[theme].name`` from Herdr's own ``config.toml``, if present."""

    target = path or herdr_config_path()
    try:
        data = tomllib.loads(target.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    theme = data.get("theme")
    if isinstance(theme, dict):
        name = theme.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


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
    backend = data.get("backend", Settings.backend)
    if not isinstance(backend, str) or backend not in BACKENDS:
        allowed = ", ".join(repr(item) for item in BACKENDS)
        raise SettingsError(f"Invalid {path}: backend must be one of {allowed}")
    start_mode = data.get("start_mode", Settings.start_mode)
    if not isinstance(start_mode, str) or start_mode not in START_MODES:
        allowed = ", ".join(repr(mode) for mode in START_MODES)
        raise SettingsError(f"Invalid {path}: start_mode must be one of {allowed}")
    theme = data.get("theme", Settings.theme)
    if not isinstance(theme, str) or not theme.strip():
        raise SettingsError(f"Invalid {path}: theme must be a non-empty string")
    return Settings(backend=backend, start_mode=start_mode, theme=theme.strip())
