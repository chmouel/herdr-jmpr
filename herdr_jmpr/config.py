"""Workspace labels: ``names.tsv`` overrides for directory names."""

from __future__ import annotations

import os
from pathlib import Path

NAMES_FILE = "names.tsv"


class NamesError(Exception):
    """A names.tsv line could not be parsed."""


def normalize_path(path: str) -> str:
    if path.startswith("~"):
        path = os.environ.get("HOME", "") + path[1:]
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def names_path() -> Path:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / NAMES_FILE
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "herdr" / "plugins" / "config" / "herdr-jmpr" / NAMES_FILE


def load_names(path: Path | None = None) -> dict[str, str]:
    path = path or names_path()
    names: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return names
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise NamesError(f"Invalid names.tsv line {lineno}: {line}")
        names[normalize_path(parts[0].strip())] = parts[1].strip()
    return names


def workspace_label(path: str, names: dict[str, str]) -> str:
    current = normalize_path(path)
    while True:
        if current in names:
            return names[current]
        if current in ("", "/"):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.basename(path.rstrip("/")) or path
