"""Workspace mapping state: canonical dir, legacy import, lock transaction."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .config import normalize_path, workspace_label
from .frecency import workspace_worktree_path

TOKEN_PATH = "jmpr_path"
# Herdr caps metadata token values at 80 characters. A value at the cap may be
# truncated, so it is never trusted as an exact path.
TOKEN_CAP = 80


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
    return Path(base) / "herdr" / "plugins" / "state" / "herdr-jmpr"


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


def read_live_mapping(
    path: Path, instance: str, workspaces: list[dict] | None = None
) -> dict[str, str]:
    """Saved mapping for the running server, or an empty one after a restart."""

    try:
        return read_live_mapping_strict(path, instance, workspaces)
    except StateError:
        return {}


def read_live_mapping_strict(
    path: Path, instance: str, workspaces: list[dict] | None = None
) -> dict[str, str]:
    """Same as :func:`read_live_mapping` but propagates an untrusted file.

    ``instance`` identifies the server that wrote the file. Pass ``workspaces``
    to refresh the mapping from the live snapshot: workspace tokens win over the
    file, and stale workspace ids are dropped.
    """

    state = read_mapping(path)
    mapping = dict(state["workspaces"]) if state.get("instance") == instance else {}
    if workspaces is None:
        return mapping
    live_ids = {
        str(workspace.get("workspace_id"))
        for workspace in workspaces
        if isinstance(workspace, dict)
    }
    mapping = {key: value for key, value in mapping.items() if key in live_ids}
    return overlay_tokens(mapping, workspaces)


def overlay_tokens(
    mapping: dict[str, str], workspaces: list[dict]
) -> dict[str, str]:
    """Fill gaps in a file mapping from workspace tokens.

    The file is written under the same lock before the token report, so it is
    never older than the token. On a conflict the durable value wins: the token
    publish failed or the process died between the two writes.
    """

    for workspace_id, token_path in mapping_from_tokens(workspaces).items():
        mapping.setdefault(workspace_id, token_path)
    return mapping


def mapping_from_tokens(workspaces: list[dict]) -> dict[str, str]:
    """Project paths reported by this plugin as workspace metadata tokens.

    Values at Herdr's 80-character cap are skipped: they may be truncated, and
    claiming one would shadow the pane-cwd and worktree fallbacks.
    """

    mapping: dict[str, str] = {}
    for workspace in workspaces:
        if not isinstance(workspace, dict):
            continue
        workspace_id = workspace.get("workspace_id")
        tokens = workspace.get("tokens")
        if not workspace_id or not isinstance(tokens, dict):
            continue
        path = tokens.get(TOKEN_PATH)
        if isinstance(path, str) and path and len(path) < TOKEN_CAP:
            mapping[str(workspace_id)] = path
    return mapping


def recover(
    state: dict,
    socket_path: str,
    instance: str,
    live_ids: set[str],
    tokens: dict[str, str] | None = None,
) -> dict:
    workspaces = dict(state.get("workspaces") or {})
    if state.get("instance") != instance:
        # A cold restart keeps the workspace ids but drops server-side tokens,
        # so the saved session is still the only source of restored identity.
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
    for workspace_id, target in (tokens or {}).items():
        if workspace_id in live_ids:
            workspaces.setdefault(workspace_id, target)
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
    normalized_path = normalize_path(path)
    close_ids: list[str] = []
    for workspace in workspaces:
        workspace_id = workspace.get("workspace_id")
        if workspace_worktree_path(workspace) == normalized_path:
            close_ids.append(workspace_id)
            continue
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
