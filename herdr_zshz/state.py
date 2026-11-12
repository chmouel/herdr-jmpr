"""Workspace mapping state: canonical dir, legacy import, lock transaction."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .config import workspace_label


class StateError(Exception):
    """Raised when a state file exists but cannot be trusted."""

    def __init__(self, path: Path):
        super().__init__(str(path))
        self.path = path


def state_dir() -> Path:
    configured = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "herdr" / "plugins" / "state" / "herdr-zshz"


def legacy_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "herdr" / "zsh-jump"


def socket_instance(socket_path: str) -> str:
    stat = os.stat(socket_path)
    return f"{stat.st_dev}:{stat.st_ino}"


def state_file(socket_path: str) -> Path:
    digest = hashlib.sha256(socket_path.encode()).hexdigest()
    return state_dir() / f"{digest}.json"


def lock_file(socket_path: str) -> Path:
    return Path(str(state_file(socket_path)) + ".lock")


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise StateError(path) from error
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("instance"), str)
        or not isinstance(data.get("workspaces"), dict)
        or not all(isinstance(value, str) for value in data["workspaces"].values())
    ):
        raise StateError(path)
    return data


def read_mapping(path: Path) -> dict[str, dict]:
    """Read the file, importing a legacy mapping non-destructively if needed."""

    if path.exists():
        return _load(path)
    legacy = legacy_state_dir() / path.name
    if legacy.exists():
        return _load(legacy)
    return {"instance": "", "workspaces": {}}


def read_live_mapping(path: Path, instance: str) -> dict[str, str]:
    """Saved mapping for the running server, or an empty one after a restart."""

    try:
        return read_live_mapping_strict(path, instance)
    except StateError:
        return {}


def read_live_mapping_strict(path: Path, instance: str) -> dict[str, str]:
    """Same as :func:`read_live_mapping` but propagates an untrusted file."""

    state = read_mapping(path)
    if state.get("instance") != instance:
        return {}
    return dict(state["workspaces"])


def recover(
    state: dict,
    socket_path: str,
    instance: str,
    live_ids: set[str],
) -> dict:
    workspaces = dict(state.get("workspaces") or {})
    if state.get("instance") != instance:
        session = Path(os.path.dirname(socket_path)) / "session.json"
        saved: list = []
        try:
            if session.is_file():
                payload = json.loads(session.read_text())
                if isinstance(payload.get("workspaces"), list):
                    saved = payload["workspaces"]
        except (OSError, ValueError):
            saved = []
        workspaces = {
            workspace_id: target
            for workspace_id, target in workspaces.items()
            if any(
                isinstance(item, dict)
                and item.get("id") == workspace_id
                and item.get("identity_cwd") == target
                for item in saved
            )
        }
    workspaces = {
        workspace_id: target
        for workspace_id, target in workspaces.items()
        if workspace_id in live_ids
    }
    return {"instance": instance, "workspaces": workspaces}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, prefix=path.name + ".", suffix=".tmp"
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def snapshot_close_targets(
    path: str,
    workspaces: list[dict],
    panes: list[dict],
    saved_mapping: dict[str, str],
    names: dict[str, str],
) -> tuple[list[str], set[tuple[str, str, str]], list[str]]:
    label = workspace_label(path, names)
    close_ids: list[str] = []
    for workspace in workspaces:
        workspace_id = workspace.get("workspace_id")
        if workspace.get("label") != label:
            continue
        if workspace_id in saved_mapping:
            if saved_mapping[workspace_id] == path:
                close_ids.append(workspace_id)
        elif any(
            pane.get("workspace_id") == workspace_id and pane.get("cwd") == path
            for pane in panes
        ):
            close_ids.append(workspace_id)
    agent_tuples: set[tuple[str, str, str]] = set()
    for pane in panes:
        if pane.get("workspace_id") in close_ids and pane.get("agent") is not None:
            agent_tuples.add(
                (pane["workspace_id"], pane.get("pane_id", ""), str(pane["agent"]))
            )
    agent_names = sorted({agent for _, _, agent in agent_tuples})
    return close_ids, agent_tuples, agent_names
