"""zsh-z and zoxide data parsing, frecency ranking and picker bucket ordering."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import normalize_path, workspace_label

CURRENT_MARK = "◆"
OPEN_MARK = "●"
TREE_BRANCH = "├─ "
TREE_LAST = "└─ "


@dataclass(frozen=True)
class TabEntry:
    workspace_id: str
    tab_id: str
    label: str
    number: int = 0
    active: bool = False
    focused: bool = False
    agents: tuple[str, ...] = ()
    agent_status: str = ""

AGENT_ICONS: dict[str, str] = {
    "copilot": "\uec1e",
    "github-copilot": "\uec1e",
    "ghcs": "\uec1e",
    "claude": "\u273b",
    "claude-code": "\u273b",
    "codex": "\U000f0626",
    "pi": "\u03c0",
    "gemini": "\u2726",
    "opencode": "\U000f018d",
    "open-code": "\U000f018d",
    "cursor": "\U000f01c0",
    "aider": "\U000f03ea",
    "amp": "\u26a1",
    "grok": "\U0001d54f",
    "devin": "\U000f06a9",
    "cline": "\U000f06a9",
    "goose": "\U000f06a9",
    "kimi": "\U000f06a9",
    "kiro": "\U000f06a9",
    "droid": "\U000f06a9",
    "hermes": "\U000f06a9",
    "kilo": "\U000f06a9",
    "qwen": "\U000f06a9",
}
DEFAULT_AGENT_ICON = "\U000f06a9"


def agent_icon(name: str) -> str:
    """Return the icon for a Herdr agent name."""

    return AGENT_ICONS.get(name.strip().lower(), DEFAULT_AGENT_ICON)


def format_agent_badges(agents: tuple[str, ...]) -> str:
    """Format active agents as compact icons with counts (e.g. ``2*π``)."""

    if not agents:
        return ""
    parts: list[str] = []
    for name, count in Counter(agents).items():
        icon = agent_icon(name)
        parts.append(f"{count}*{icon}" if count > 1 else icon)
    return " ".join(parts)


def format_tab_agent_badge(agents: tuple[str, ...], status: str = "") -> str:
    """Format a tab's detected agent(s) with icon, name, and optional status."""

    if not agents:
        return ""
    parts: list[str] = []
    for name, count in Counter(agents).items():
        icon = agent_icon(name)
        label = f"{count}*{icon} {name}" if count > 1 else f"{icon} {name}"
        parts.append(label)
    badge = " ".join(parts)
    clean_status = status.strip().lower()
    if clean_status and clean_status not in ("idle", "none"):
        badge = f"{badge} · {clean_status}"
    return badge


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


def read_zoxide_history() -> list[str]:
    """Return existing directories from ``zoxide query -l``, best rank first."""

    binary = shutil.which("zoxide")
    if not binary:
        return []
    try:
        completed = subprocess.run(
            [binary, "query", "-l"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line]


def _read_zshz_history(path: Path, now: float) -> list[str]:
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


def read_history(
    path: Path | None = None,
    now: float | None = None,
    source: str = "auto",
) -> list[str]:
    """Return directories from zsh-z and/or zoxide, best rank first."""

    if source == "zoxide":
        return read_zoxide_history()
    now = time.time() if now is None else now
    entries = _read_zshz_history(path or data_path(), now)
    if entries or source == "zshz" or path is not None:
        return entries
    return read_zoxide_history()


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


def workspace_worktree_path(workspace: dict) -> str | None:
    """Return the normalized checkout path for a Herdr worktree workspace."""

    worktree = workspace.get("worktree")
    if not isinstance(worktree, dict):
        return None
    checkout = worktree.get("checkout_path")
    if isinstance(checkout, str) and checkout:
        return normalize_path(checkout)
    return None


def open_paths(
    workspaces: list[dict],
    panes: list[dict],
    saved_mapping: dict[str, str],
    names: dict[str, str],
) -> set[str]:
    live = {w.get("workspace_id"): w.get("label") for w in workspaces}
    opened: set[str] = set()
    for workspace in workspaces:
        workspace_id = workspace.get("workspace_id")
        label = workspace.get("label")
        mapped = saved_mapping.get(workspace_id)
        if mapped and workspace_label(mapped, names) == label:
            opened.add(mapped)
            continue
        checkout = workspace_worktree_path(workspace)
        if checkout:
            opened.add(checkout)
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
    cur_workspace = next(
        (w for w in workspaces if w.get("workspace_id") == cur_ws), None
    )
    cur_label = cur_workspace.get("label", "") if cur_workspace else ""
    cur_worktree = workspace_worktree_path(cur_workspace) if cur_workspace else None
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
        if (
            cur_mapped == directory
            or cur_worktree == normalize_path(directory)
            or (not cur_mapped and cur_label == workspace_label(directory, names))
        ):
            return None
        if directory in (current, "/"):
            break
        directory = os.path.dirname(directory)
    return current


def _label_or_fallback_cwds(
    label: object,
    cwds: set[str] | tuple,
    names: dict[str, str],
    saved: str | None = None,
) -> list[str]:
    """Pane cwds matching the workspace label, else the saved mapping cwd when
    still open, else the first cwd so renamed or custom-labelled workspaces are
    not dropped."""

    ordered = sorted(cwds)
    matched = [cwd for cwd in ordered if workspace_label(cwd, names) == label]
    if matched:
        return matched
    if saved in ordered:
        return [saved]
    return ordered[:1]


def agents_by_path(
    workspaces: list[dict],
    panes: list[dict],
    saved_mapping: dict[str, str],
    names: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    """Map open workspace paths to sorted active agent names (preserving counts)."""

    ws_agents: dict[str, list[str]] = {}
    ws_cwds: dict[str, set[str]] = {}
    for pane in panes:
        ws_id = pane.get("workspace_id")
        if not ws_id:
            continue
        cwd = pane.get("cwd")
        if cwd:
            ws_cwds.setdefault(str(ws_id), set()).add(cwd)
        agent = pane.get("agent")
        if agent is not None:
            ws_agents.setdefault(str(ws_id), []).append(str(agent))

    by_path: dict[str, list[str]] = {}
    for workspace in workspaces:
        ws_id = workspace.get("workspace_id")
        label = workspace.get("label")
        if not ws_id or str(ws_id) not in ws_agents:
            continue
        agents = ws_agents[str(ws_id)]
        mapped = saved_mapping.get(ws_id)
        if mapped and workspace_label(mapped, names) == label:
            by_path.setdefault(mapped, []).extend(agents)
            continue
        checkout = workspace_worktree_path(workspace)
        if checkout:
            by_path.setdefault(checkout, []).extend(agents)
            continue
        for cwd in _label_or_fallback_cwds(label, ws_cwds.get(str(ws_id), ()), names, mapped):
            by_path.setdefault(cwd, []).extend(agents)
    return {path: tuple(sorted(agents)) for path, agents in by_path.items()}


_AGENT_STATUS_PRIORITY = {"working": 0, "waiting": 1, "attention": 1, "idle": 2}


def _pick_agent_status(statuses: list[str]) -> str:
    cleaned = [s.strip().lower() for s in statuses if s and s.strip()]
    if not cleaned:
        return ""
    cleaned.sort(key=lambda s: (_AGENT_STATUS_PRIORITY.get(s, 3), s))
    return cleaned[0]


def _pane_title(pane: dict) -> str:
    tokens = pane.get("tokens")
    if isinstance(tokens, dict):
        title = str(tokens.get("tl") or "").strip()
        if title:
            return title
    return str(pane.get("terminal_title_stripped") or "").strip()


def _workspace_paths(
    workspace: dict,
    ws_cwds: dict[str, set[str]],
    saved_mapping: dict[str, str],
    names: dict[str, str],
) -> list[str]:
    ws_id = workspace.get("workspace_id")
    label = workspace.get("label")
    if not ws_id:
        return []
    mapped = saved_mapping.get(ws_id)
    if mapped and workspace_label(mapped, names) == label:
        return [mapped]
    checkout = workspace_worktree_path(workspace)
    if checkout:
        return [checkout]
    return _label_or_fallback_cwds(label, ws_cwds.get(str(ws_id), ()), names, mapped)


def tabs_by_path(
    workspaces: list[dict],
    tabs: list[dict] | None,
    panes: list[dict],
    saved_mapping: dict[str, str],
    names: dict[str, str],
) -> dict[str, tuple[TabEntry, ...]]:
    """Map open workspace paths to ordered :class:`TabEntry` tuples with agent detection."""

    ws_cwds: dict[str, set[str]] = {}
    panes_by_tab: dict[tuple[str, str], list[dict]] = {}
    for pane in panes:
        ws_id = pane.get("workspace_id")
        if not ws_id:
            continue
        ws_key = str(ws_id)
        cwd = pane.get("cwd")
        if cwd:
            ws_cwds.setdefault(ws_key, set()).add(cwd)
        tab_id = pane.get("tab_id")
        if tab_id:
            panes_by_tab.setdefault((ws_key, str(tab_id)), []).append(pane)

    tabs_by_ws: dict[str, list[dict]] = {}
    for tab in tabs or ():
        ws_id = tab.get("workspace_id")
        tab_id = tab.get("tab_id")
        if ws_id and tab_id:
            tabs_by_ws.setdefault(str(ws_id), []).append(tab)

    by_path: dict[str, list[TabEntry]] = {}
    for workspace in workspaces:
        ws_id = workspace.get("workspace_id")
        if not ws_id:
            continue
        ws_key = str(ws_id)
        paths = _workspace_paths(workspace, ws_cwds, saved_mapping, names)
        if not paths:
            continue
        active_tab_id = str(workspace.get("active_tab_id") or "")
        raw_tabs = tabs_by_ws.get(ws_key)
        entries: list[TabEntry] = []
        if raw_tabs:
            ordered = sorted(
                raw_tabs,
                key=lambda t: (int(t.get("number") or 0), str(t.get("tab_id") or "")),
            )
            for tab in ordered:
                tab_id = str(tab.get("tab_id") or "")
                tab_panes = panes_by_tab.get((ws_key, tab_id), [])
                agents = tuple(
                    sorted(
                        str(pane["agent"])
                        for pane in tab_panes
                        if pane.get("agent") is not None
                    )
                )
                statuses = [str(tab.get("agent_status") or "")]
                statuses.extend(
                    str(pane.get("agent_status") or "")
                    for pane in tab_panes
                    if pane.get("agent") is not None
                )
                agent_status = _pick_agent_status(statuses) if agents else ""
                label = str(tab.get("label") or "").strip()
                if not label:
                    for pane in tab_panes:
                        title = _pane_title(pane)
                        if title:
                            label = title
                            break
                if not label:
                    number = int(tab.get("number") or 0)
                    label = f"tab {number}" if number else tab_id
                focused = bool(tab.get("focused"))
                entries.append(
                    TabEntry(
                        workspace_id=ws_key,
                        tab_id=tab_id,
                        label=label,
                        number=int(tab.get("number") or 0),
                        active=focused or (bool(active_tab_id) and tab_id == active_tab_id),
                        focused=focused,
                        agents=agents,
                        agent_status=agent_status,
                    )
                )
        else:
            # Fallback when tab list wasn't provided: synthesize from panes.
            tab_ids = sorted(
                {tab_id for (pane_ws, tab_id) in panes_by_tab if pane_ws == ws_key}
            )
            for idx, tab_id in enumerate(tab_ids, 1):
                tab_panes = panes_by_tab.get((ws_key, tab_id), [])
                agents = tuple(
                    sorted(
                        str(pane["agent"])
                        for pane in tab_panes
                        if pane.get("agent") is not None
                    )
                )
                statuses = [
                    str(pane.get("agent_status") or "")
                    for pane in tab_panes
                    if pane.get("agent") is not None
                ]
                agent_status = _pick_agent_status(statuses) if agents else ""
                label = ""
                for pane in tab_panes:
                    title = _pane_title(pane)
                    if title:
                        label = title
                        break
                if not label:
                    label = f"tab {idx}"
                focused = any(bool(pane.get("focused")) for pane in tab_panes)
                entries.append(
                    TabEntry(
                        workspace_id=ws_key,
                        tab_id=tab_id,
                        label=label,
                        number=idx,
                        active=focused or (bool(active_tab_id) and tab_id == active_tab_id),
                        focused=focused,
                        agents=agents,
                        agent_status=agent_status,
                    )
                )
        if not entries:
            continue
        for target_path in paths:
            existing = by_path.setdefault(target_path, [])
            seen_ids = {item.tab_id for item in existing}
            for entry in entries:
                if entry.tab_id not in seen_ids:
                    existing.append(entry)
                    seen_ids.add(entry.tab_id)
    return {path: tuple(items) for path, items in by_path.items()}


def _cached_git_root(path: str, cache: dict[str, str | None]) -> str | None:
    current = normalize_path(path)
    visited: list[str] = []
    found: str | None = None
    while current and current != "/":
        if current in cache:
            found = cache[current]
            break
        visited.append(current)
        if os.path.exists(os.path.join(current, ".git")):
            found = current
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for entry in visited:
        cache[entry] = found
    return found


def rank_paths(
    history: list[str],
    current: str | None,
    opened: set[str],
    show_all: bool,
) -> list[str]:
    """Order paths: current project first, open workspaces next, then frecency."""

    all_paths: list[str] = []
    in_all: set[str] = set()
    git_paths: list[str] = []
    in_git: set[str] = set()
    root_cache: dict[str, str | None] = {}
    if current:
        all_paths.append(current)
        in_all.add(current)
        git_paths.append(current)
        in_git.add(current)
    for directory in history:
        if not os.path.isdir(directory) or directory == current:
            continue
        if directory not in in_all:
            in_all.add(directory)
            all_paths.append(directory)
        if directory in opened:
            root = directory
        else:
            root = _cached_git_root(directory, root_cache)
            if root is None:
                continue
        if root in in_git:
            continue
        in_git.add(root)
        git_paths.append(root)
    for directory in sorted(opened):
        if not os.path.isdir(directory) or directory == current:
            continue
        if directory not in in_all:
            in_all.add(directory)
            all_paths.append(directory)
        if directory not in in_git:
            in_git.add(directory)
            git_paths.append(directory)
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
