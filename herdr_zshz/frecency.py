"""zsh-z data parsing, frecency ranking and picker bucket ordering."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .config import normalize_path, workspace_label

CURRENT_MARK = "◆"
OPEN_MARK = "●"


def data_path() -> Path:
    configured = os.environ.get("ZSHZ_DATA") or os.environ.get("_Z_DATA")
    if configured:
        return Path(os.path.expanduser(configured))
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    candidate = Path(cache) / "zsh" / "zsh-cache-z"
    if candidate.exists():
        return candidate
    return Path.home() / ".z"


def score(rank: float, timestamp: float, now: float) -> float:
    return 10000 * rank * (3.75 / ((0.0001 * (now - timestamp) + 1) + 0.25))


def read_history(path: Path | None = None, now: float | None = None) -> list[str]:
    """Return existing directories from the z data file, best rank first."""

    path = path or data_path()
    now = time.time() if now is None else now
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    ranked: list[tuple[float, str]] = []
    for line in lines:
        fields = line.split("|")
        if len(fields) < 3 or not fields[0]:
            continue
        try:
            rank = float(fields[1])
            timestamp = float(fields[2])
        except ValueError:
            continue
        ranked.append((score(rank, timestamp, now), fields[0]))
    ranked.sort(key=lambda entry: -entry[0])
    return [entry[1] for entry in ranked]


def git_root(path: str) -> str | None:
    current = normalize_path(path)
    while current and current != "/":
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def open_paths(
    workspaces: list[dict],
    panes: list[dict],
    saved_mapping: dict[str, str],
    names: dict[str, str],
) -> set[str]:
    live = {w.get("workspace_id"): w.get("label") for w in workspaces}
    opened: set[str] = set()
    for workspace_id, label in live.items():
        mapped = saved_mapping.get(workspace_id)
        if mapped and workspace_label(mapped, names) == label:
            opened.add(mapped)
    for pane in panes:
        cwd = pane.get("cwd")
        if not cwd or saved_mapping.get(pane.get("workspace_id")):
            continue
        if live.get(pane.get("workspace_id")) == workspace_label(cwd, names):
            opened.add(cwd)
    return opened


def current_project(
    active_pane_id: str,
    panes: list[dict],
    workspaces: list[dict],
    saved_mapping: dict[str, str],
    names: dict[str, str],
) -> str | None:
    pane = next(
        (p for p in panes if p.get("pane_id") == active_pane_id and p.get("cwd")), None
    )
    if pane is None:
        return None
    cur_ws = pane.get("workspace_id")
    cur_label = next(
        (w.get("label") for w in workspaces if w.get("workspace_id") == cur_ws), ""
    )
    cur_cwd = pane["cwd"]
    if not os.path.isdir(cur_cwd):
        return None
    home = os.environ.get("HOME", "")
    current = git_root(cur_cwd)
    if current is None:
        if cur_cwd in (home, "/"):
            return None
        current = cur_cwd
    cur_mapped = saved_mapping.get(cur_ws)
    directory = cur_cwd
    while current:
        if cur_mapped == directory or (
            not cur_mapped and cur_label == workspace_label(directory, names)
        ):
            return None
        if directory in (current, "/"):
            break
        directory = os.path.dirname(directory)
    return current


def rank_paths(
    history: list[str],
    current: str | None,
    opened: set[str],
    show_all: bool,
) -> list[str]:
    """Order paths as zsh-jump does: current, open projects, then frecency."""

    all_paths: list[str] = []
    git_paths: list[str] = []
    in_git: set[str] = set()
    if current:
        all_paths.append(current)
        git_paths.append(current)
        in_git.add(current)
    for directory in history:
        if not os.path.isdir(directory) or directory == current:
            continue
        all_paths.append(directory)
        if directory in opened:
            root = directory
        else:
            root = git_root(directory)
            if root is None:
                continue
        if root in in_git:
            continue
        in_git.add(root)
        git_paths.append(root)
    paths = all_paths if show_all else git_paths
    top = [path for path in paths if path == current]
    rest = [path for path in paths if path != current]
    open_first = [path for path in rest if path in opened]
    others = [path for path in rest if path not in opened]
    return top + open_first + others


def marker(path: str, current: str | None, opened: set[str]) -> str:
    if path == current:
        return CURRENT_MARK
    if path in opened:
        return OPEN_MARK
    return " "


def display_path(path: str) -> str:
    home = os.environ.get("HOME", "")
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home) :]
    return path
