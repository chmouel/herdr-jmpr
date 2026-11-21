"""Command line entry point for the herdr-jmpr plugin."""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import NamesError, load_names, normalize_path, workspace_label
from .frecency import (
    agents_by_path,
    current_project,
    open_paths,
    rank_paths,
    read_history,
    tabs_by_path,
    workspace_worktree_path,
)
from .herdr import Herdr, HerdrError, move_tab_layout
from .picker import (
    ALL_ROWS_ENV,
    BACKEND_ENV,
    GIT_ROWS_ENV,
    OPEN_ROWS_ENV,
    PickerError,
    Result,
    resolve_backend,
    run_fzf,
    run_gum,
    run_skim,
    write_row_files,
)
from .settings import START_MODES, Settings, SettingsError, load_settings
from .state import (
    StateError,
    lock_file,
    mapping_from_tokens,
    read_live_mapping,
    read_live_mapping_strict,
    read_mapping,
    recover,
    snapshot_close_targets,
    socket_instance,
    state_dir,
    state_file,
    write_state,
)

USAGE = (
    "usage: herdr-jmpr [--move-pane|--move-tab] [--all] [--tabs|--no-tabs] "
    "[--vim|--no-vim|--normal|--insert|--mode <insert|normal>] [--backend <backend>]"
)
PLUGIN_ID = "herdr-jmpr"
SHOW_TABS_ENV = "HERDR_JMPR_SHOW_TABS"
START_MODE_ENV = "HERDR_JMPR_START_MODE"


class Failure(Exception):
    pass


def plugin_context() -> dict:
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if not raw:
        return {}
    try:
        context = json.loads(raw)
    except ValueError:
        return {}
    return context if isinstance(context, dict) else {}


def _context_id(context: dict, *keys: str) -> str:
    for key in keys:
        value = context.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for nested in ("pane_id", "workspace_id", "tab_id", "id"):
                candidate = value.get(nested)
                if isinstance(candidate, str) and candidate:
                    return candidate
    return ""


def active_pane_id() -> str:
    for variable in ("HERDR_ACTIVE_PANE_ID", "HERDR_PANE_ID"):
        value = os.environ.get(variable)
        if value:
            return value
    return _context_id(plugin_context(), "focused_pane", "pane", "pane_id")


def active_workspace_id() -> str:
    for variable in ("HERDR_ACTIVE_WORKSPACE_ID", "HERDR_WORKSPACE_ID"):
        value = os.environ.get(variable)
        if value:
            return value
    return _context_id(plugin_context(), "workspace", "workspace_id")


def active_tab_id() -> str:
    for variable in ("HERDR_ACTIVE_TAB_ID", "HERDR_TAB_ID"):
        value = os.environ.get(variable)
        if value:
            return value
    return _context_id(plugin_context(), "tab", "tab_id")


def ignore_hangup() -> None:
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, OSError, ValueError):
        pass


def log_error(message: str) -> None:
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "last-error.log", "a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def fail(message: str) -> int:
    log_error(message)
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except OSError:
        pass
    try:
        if os.isatty(0):
            sys.stderr.write("[press a key] ")
            sys.stderr.flush()
            sys.stdin.read(1)
    except OSError:
        pass
    return 1


def confirm(prompt: str) -> bool:
    try:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        answer = sys.stdin.read(1)
    except OSError:
        return False
    try:
        sys.stderr.write("\n")
        sys.stderr.flush()
    except OSError:
        pass
    return answer in ("y", "Y")


def socket_path(herdr: Herdr) -> str:
    configured = os.environ.get("HERDR_SOCKET_PATH")
    if configured:
        return configured
    return herdr.status_socket()


def open_picker(
    move_mode: str | None,
    show_all: bool,
    backend_override: str | None = None,
    show_tabs_override: bool | None = None,
    start_mode_override: str | None = None,
) -> int:
    if move_mode == "pane":
        entrypoint = "move-pane"
    elif move_mode == "tab":
        entrypoint = "move-tab"
    elif show_all:
        entrypoint = "jump-all"
    else:
        entrypoint = "jump"
    herdr = Herdr()
    command = [
        herdr.executable,
        "plugin",
        "pane",
        "open",
        "--plugin",
        PLUGIN_ID,
        "--entrypoint",
        entrypoint,
        "--placement",
        "popup",
    ]
    pane_id = active_pane_id()
    workspace_id = active_workspace_id()
    if pane_id:
        command += ["--env", f"HERDR_ACTIVE_PANE_ID={pane_id}"]
    if workspace_id:
        command += ["--workspace", workspace_id]
        command += ["--env", f"HERDR_ACTIVE_WORKSPACE_ID={workspace_id}"]
    tab_id = active_tab_id()
    if tab_id:
        command += ["--env", f"HERDR_ACTIVE_TAB_ID={tab_id}"]
    if backend_override:
        command += ["--env", f"{BACKEND_ENV}={backend_override}"]
    if show_tabs_override is not None:
        command += ["--env", f"{SHOW_TABS_ENV}={'1' if show_tabs_override else '0'}"]
    if start_mode_override is not None:
        command += ["--env", f"{START_MODE_ENV}={start_mode_override}"]
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        try:
            sys.stderr.write(completed.stderr or completed.stdout)
        except OSError:
            pass
    return completed.returncode


def move_source(result, target: str, label: str, herdr: Herdr) -> tuple[str, str]:
    if result.kind == "move-pane":
        if target:
            pane = herdr.pane_move(result.src_pane, "--new-tab", "--workspace", target)
        else:
            pane = herdr.pane_move(result.src_pane, "--new-workspace", "--label", label)
        return pane.get("workspace_id", ""), pane.get("tab_id", "")

    return move_tab_layout(
        herdr,
        result.src_layout or {},
        target,
        label,
        result.tab_label or "moved",
    )


def resolve_and_move(
    result,
    target: str,
    label: str,
    state: dict,
    workspaces: list[dict],
    panes: list[dict],
    herdr: Herdr,
) -> tuple[str, str]:
    mapping = state["workspaces"]

    mapped = next(
        (
            workspace
            for workspace in workspaces
            if workspace.get("label") == label
            and mapping.get(workspace.get("workspace_id")) == target
        ),
        None,
    )
    if mapped is not None:
        return str(mapped["workspace_id"]), ""

    normalized_target = normalize_path(target)
    worktree_match = next(
        (
            workspace
            for workspace in workspaces
            if workspace_worktree_path(workspace) == normalized_target
        ),
        None,
    )
    if worktree_match is not None:
        return str(worktree_match["workspace_id"]), ""

    for workspace in workspaces:
        workspace_id = workspace.get("workspace_id")
        if workspace.get("label") != label or workspace_id in mapping:
            continue
        tabs = [
            pane.get("tab_id")
            for pane in panes
            if pane.get("workspace_id") == workspace_id and pane.get("cwd") == target
        ]
        active = workspace.get("active_tab_id")
        tab_id = active if active in tabs else (tabs[0] if tabs else "")
        if tab_id:
            return str(workspace_id), "" if tab_id == active else str(tab_id)

    unknown = [
        str(workspace.get("workspace_id"))
        for workspace in workspaces
        if workspace.get("label") == label
        and workspace.get("workspace_id") not in mapping
    ]
    choice = result.choice
    if choice is not None:
        kind, chosen = choice
        if kind == "use":
            candidate = next(
                (
                    workspace
                    for workspace in workspaces
                    if workspace.get("workspace_id") == chosen
                ),
                None,
            )
            if (
                candidate is None
                or candidate.get("label") != label
                or mapping.get(chosen, target) != target
            ):
                raise Failure(f"Invalid workspace selection: {chosen}")
            return str(chosen), ""
    elif unknown:
        raise Failure(
            f"Workspace state changed concurrently for {label}; please retry"
        )

    if result.kind != "jump":
        return "", ""

    workspace = herdr.workspace_create(target, label)
    return str(workspace["workspace_id"]), ""


def jump(
    result, herdr: Herdr, socket: str, instance: str, path: Path, names: dict[str, str]
) -> int:
    ignore_hangup()
    target = result.path
    label = workspace_label(target, names)
    lock_path = lock_file(socket)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        snapshot = herdr.snapshot()
        workspaces = snapshot.get("workspaces") or []
        panes = snapshot.get("panes") or []
        live_ids = {str(workspace.get("workspace_id")) for workspace in workspaces}
        state = recover(
            read_mapping(path),
            socket,
            instance,
            live_ids,
            mapping_from_tokens(workspaces),
        )
        target_ws_id = getattr(result, "target_workspace_id", "") or ""
        target_tab_id = getattr(result, "target_tab_id", "") or ""
        if result.kind == "jump" and target_tab_id and target_ws_id in live_ids:
            destination, tab_id = target_ws_id, target_tab_id
        else:
            destination, tab_id = resolve_and_move(
                result, target, label, state, workspaces, panes, herdr
            )
            if result.kind == "jump" and target_tab_id and not tab_id:
                tab_id = target_tab_id
        if result.kind != "jump":
            tab_id = ""
            if destination != result.src_ws:
                destination, tab_id = move_source(result, destination, label, herdr)
        state["workspaces"][destination] = target
        write_state(path, state)
        publish_workspace_path(herdr, destination, target)
    except BaseException:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
        raise
    fcntl.flock(handle, fcntl.LOCK_UN)
    os.close(handle)
    focus_workspace(herdr, destination, tab_id)
    return 0


def publish_workspace_path(herdr: Herdr, workspace_id: str, path: str) -> None:
    """Best-effort workspace token update; jump and focus must still work."""

    if not workspace_id or not path:
        return
    try:
        herdr.report_workspace_path(workspace_id, path)
    except (HerdrError, OSError):
        pass


def focus_workspace(herdr: Herdr, workspace_id: str, tab_id: str = "") -> None:
    if not workspace_id:
        return
    herdr.workspace_focus(workspace_id)
    if tab_id:
        herdr.tab_focus(tab_id)


def _safe_tabs(herdr: Herdr) -> list[dict]:
    try:
        return herdr.tabs()
    except HerdrError:
        return []


def _fetch_herdr_state(herdr: Herdr) -> tuple[list[dict], list[dict], list[dict]]:
    snapshot = herdr.snapshot()
    return (
        snapshot.get("workspaces") or [],
        snapshot.get("panes") or [],
        snapshot.get("tabs") or [],
    )


def _resolve_show_tabs(settings: Settings, override: bool | None) -> bool:
    if override is not None:
        return override
    env_val = os.environ.get(SHOW_TABS_ENV, "").strip().lower()
    if env_val in ("0", "false", "no", "off"):
        return False
    if env_val in ("1", "true", "yes", "on"):
        return True
    return settings.show_tabs


def _resolve_start_mode(settings: Settings, override: str | None) -> str:
    requested = override or os.environ.get(START_MODE_ENV, "").strip() or settings.start_mode
    if requested not in START_MODES:
        allowed = ", ".join(repr(mode) for mode in START_MODES)
        raise PickerError(f"Unknown start mode {requested!r}; expected one of {allowed}")
    return requested


def interactive(
    move_mode: str | None,
    show_all: bool,
    settings: Settings,
    backend_override: str | None = None,
    show_tabs_override: bool | None = None,
    start_mode_override: str | None = None,
) -> int:
    backend = resolve_backend(settings.backend, backend_override)
    show_tabs = _resolve_show_tabs(settings, show_tabs_override)
    start_mode = _resolve_start_mode(settings, start_mode_override)
    names = load_names()
    herdr = Herdr()
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        if backend == "textual":
            # Warm the Textual import while the herdr and history I/O run.
            pool.submit(importlib.import_module, f"{__package__}.tui")
        socket_future = pool.submit(socket_path, herdr)
        snapshot_future = pool.submit(herdr.snapshot)
        history_future = pool.submit(read_history, source=settings.history)
        socket = socket_future.result()
        snapshot = snapshot_future.result()
        workspaces = snapshot.get("workspaces") or []
        panes = snapshot.get("panes") or []
        raw_tabs = snapshot.get("tabs") or []
        history = history_future.result()
    path = state_file(socket)
    instance = socket_instance(socket)
    mapping = read_live_mapping_strict(path, instance, workspaces)
    pane_id = active_pane_id()
    opened = open_paths(workspaces, panes, mapping, names)
    current = current_project(pane_id, panes, workspaces, mapping, names)
    agents = agents_by_path(workspaces, panes, mapping, names)
    tabs = tabs_by_path(workspaces, raw_tabs, panes, mapping, names)
    paths_git = rank_paths(history, current, opened, False)
    paths_all = rank_paths(history, current, opened, True)
    mapping_reader = lambda workspaces=None: read_live_mapping(  # noqa: E731
        path, instance, workspaces
    )

    result: Result | None
    focus_after_exit: tuple[str, str] | None = None
    if backend == "fzf":
        result = run_fzf(
            paths_git=paths_git,
            paths_all=paths_all,
            current=current,
            opened=opened,
            names=names,
            move_mode=move_mode,
            show_all=show_all,
            src_pane=pane_id,
            herdr=herdr,
            mapping_reader=mapping_reader,
            agents=agents,
            tabs=tabs,
            show_tabs=show_tabs,
        )
    elif backend == "skim":
        result = run_skim(
            paths_git=paths_git,
            paths_all=paths_all,
            current=current,
            opened=opened,
            names=names,
            move_mode=move_mode,
            show_all=show_all,
            src_pane=pane_id,
            herdr=herdr,
            mapping_reader=mapping_reader,
            history=history,
            close_callback=lambda target: close(target, names, herdr),
            agents=agents,
            tabs=tabs,
            show_tabs=show_tabs,
        )
    elif backend == "gum":
        result = run_gum(
            paths_git=paths_git,
            paths_all=paths_all,
            current=current,
            opened=opened,
            names=names,
            move_mode=move_mode,
            show_all=show_all,
            src_pane=pane_id,
            herdr=herdr,
            mapping_reader=mapping_reader,
            agents=agents,
            tabs=tabs,
            show_tabs=show_tabs,
        )
    else:
        from .tui import PickerApp

        def record_workspace(ws_id: str, target_path: str) -> None:
            lock_path = lock_file(socket)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
                live = herdr.snapshot().get("workspaces") or []
                live_ids = {str(w.get("workspace_id")) for w in live}
                live_ids.add(ws_id)
                cur = recover(
                    read_mapping(path),
                    socket,
                    instance,
                    live_ids,
                    mapping_from_tokens(live),
                )
                cur["workspaces"][ws_id] = target_path
                write_state(path, cur)
                publish_workspace_path(herdr, ws_id, target_path)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                os.close(handle)

        app = PickerApp(
            paths_git=paths_git,
            paths_all=paths_all,
            current=current,
            opened=opened,
            names=names,
            move_mode=move_mode,
            show_all=show_all,
            src_pane=pane_id,
            herdr=herdr,
            mapping_reader=mapping_reader,
            strict_mapping_reader=lambda workspaces=None: read_live_mapping_strict(
                path, instance, workspaces
            ),
            history=history,
            start_mode=start_mode,
            theme_setting=settings.theme,
            workspaces=workspaces,
            panes=panes,
            agents=agents,
            tabs=tabs,
            show_tabs=show_tabs,
            record_workspace=record_workspace,
        )
        result = app.run()
        focus_after_exit = app.focus_after_exit
    ignore_hangup()
    if result is None:
        if focus_after_exit:
            focus_workspace(herdr, *focus_after_exit)
        return 0
    return jump(result, herdr, socket, instance, path, names)


def _refresh_row_files(
    herdr: Herdr, path: Path, instance: str, names: dict[str, str]
) -> None:
    git_env = os.environ.get(GIT_ROWS_ENV)
    all_env = os.environ.get(ALL_ROWS_ENV)
    if not git_env or not all_env:
        return
    git_file = Path(git_env)
    all_file = Path(all_env)
    if not git_file.parent.is_dir() or not all_file.parent.is_dir():
        return
    open_env = os.environ.get(OPEN_ROWS_ENV)
    open_file = Path(open_env) if open_env and Path(open_env).parent.is_dir() else None
    workspaces, panes, raw_tabs = _fetch_herdr_state(herdr)
    mapping = read_live_mapping(path, instance, workspaces)
    pane_id = active_pane_id()
    opened = open_paths(workspaces, panes, mapping, names)
    current = current_project(pane_id, panes, workspaces, mapping, names)
    agents = agents_by_path(workspaces, panes, mapping, names)
    settings = load_settings()
    tabs = (
        tabs_by_path(workspaces, raw_tabs, panes, mapping, names)
        if _resolve_show_tabs(settings, None)
        else None
    )
    history = read_history(source=settings.history)
    write_row_files(
        git_file,
        all_file,
        rank_paths(history, current, opened, False),
        rank_paths(history, current, opened, True),
        current,
        opened,
        names,
        open_file=open_file,
        agents=agents,
        tabs=tabs,
    )


def close(path_argument: str, names: dict[str, str], herdr: Herdr) -> int:
    if not path_argument:
        return 0
    socket = socket_path(herdr)
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = state_file(socket)
    instance = socket_instance(socket)
    target = os.path.abspath(os.path.expanduser(path_argument))
    label = workspace_label(target, names)
    snapshot = herdr.snapshot()
    workspaces = snapshot.get("workspaces") or []
    panes = snapshot.get("panes") or []
    mapping = read_live_mapping_strict(path, instance, workspaces)
    close_ids, agent_tuples, agent_names = snapshot_close_targets(
        target, workspaces, panes, mapping, names
    )
    if not close_ids:
        _refresh_row_files(herdr, path, instance, names)
        return 0
    approved_ids = set(close_ids)
    if agent_tuples and not confirm(
        f"Close {label}? {', '.join(agent_names)} running there [y/N] "
    ):
        return 0
    snapshot = herdr.snapshot()
    workspaces = snapshot.get("workspaces") or []
    panes = snapshot.get("panes") or []
    mapping = read_live_mapping_strict(path, instance, workspaces)
    current_ids, current_tuples, current_names = snapshot_close_targets(
        target, workspaces, panes, mapping, names
    )
    if current_tuples - agent_tuples:
        try:
            sys.stderr.write(
                f"Not closing {label}: {', '.join(current_names)} started running there\n"
            )
        except OSError:
            pass
        return 0
    for workspace_id in [w for w in current_ids if w in approved_ids]:
        herdr.workspace_close(workspace_id)
    _refresh_row_files(herdr, path, instance, names)
    return 0


def _acquire_lock(socket: str) -> int:
    lock_path = lock_file(socket)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _release_lock(handle: int) -> None:
    fcntl.flock(handle, fcntl.LOCK_UN)
    os.close(handle)


def publish_state() -> int:
    """Republish workspace tokens from the durable mapping (startup hook)."""

    herdr = Herdr()
    socket = socket_path(herdr)
    instance = socket_instance(socket)
    path = state_file(socket)
    handle = _acquire_lock(socket)
    try:
        workspaces = herdr.snapshot().get("workspaces") or []
        live_ids = {str(workspace.get("workspace_id")) for workspace in workspaces}
        state = recover(
            read_mapping(path),
            socket,
            instance,
            live_ids,
            mapping_from_tokens(workspaces),
        )
        write_state(path, state)
        for workspace_id, target in state["workspaces"].items():
            publish_workspace_path(herdr, workspace_id, target)
    except StateError:
        return 0
    finally:
        _release_lock(handle)
    return 0


def closed_workspace_id(raw: str) -> str:
    """Workspace id from a plugin ``workspace.closed`` event payload."""

    try:
        payload = json.loads(raw)
    except ValueError:
        return ""

    def find(node) -> str:
        if isinstance(node, dict):
            workspace = node.get("workspace")
            if isinstance(workspace, dict):
                workspace_id = workspace.get("workspace_id")
                if isinstance(workspace_id, str) and workspace_id:
                    return workspace_id
            workspace_id = node.get("workspace_id")
            if isinstance(workspace_id, str) and workspace_id:
                return workspace_id
            for value in node.values():
                found = find(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = find(value)
                if found:
                    return found
        return ""

    return find(payload)


def workspace_closed() -> int:
    """Prune a closed workspace from durable state (event hook)."""

    workspace_id = closed_workspace_id(os.environ.get("HERDR_PLUGIN_EVENT_JSON", ""))
    if not workspace_id:
        return 0
    herdr = Herdr()
    socket = socket_path(herdr)
    path = state_file(socket)
    handle = _acquire_lock(socket)
    try:
        state = read_mapping(path)
        if workspace_id in state["workspaces"]:
            del state["workspaces"][workspace_id]
            write_state(path, state)
    except StateError:
        return 0
    finally:
        _release_lock(handle)
    return 0


def parse(
    argv: list[str],
) -> tuple[str | None, bool, bool, str | None, str | None, bool | None, str | None]:
    move_mode: str | None = None
    show_all = False
    open_picker_mode = False
    close_path: str | None = None
    backend_override: str | None = None
    show_tabs_override: bool | None = None
    start_mode_override: str | None = None
    remaining = list(argv)
    while remaining:
        argument = remaining.pop(0)
        if argument == "--move-pane":
            move_mode = "pane"
        elif argument == "--move-tab":
            move_mode = "tab"
        elif argument == "--all":
            show_all = True
        elif argument == "--tabs":
            show_tabs_override = True
        elif argument == "--no-tabs":
            show_tabs_override = False
        elif argument in ("--vim", "--normal"):
            start_mode_override = "normal"
        elif argument in ("--no-vim", "--insert"):
            start_mode_override = "insert"
        elif argument == "--mode":
            if not remaining or not remaining[0].strip():
                raise Failure(USAGE)
            start_mode_override = remaining.pop(0).strip()
        elif argument.startswith("--mode="):
            value = argument.split("=", 1)[1].strip()
            if not value:
                raise Failure(USAGE)
            start_mode_override = value
        elif argument == "open-picker":
            open_picker_mode = True
        elif argument == "--close":
            close_path = remaining.pop(0) if remaining else ""
        elif argument == "--backend":
            if not remaining or not remaining[0].strip():
                raise Failure(USAGE)
            backend_override = remaining.pop(0).strip()
        elif argument.startswith("--backend="):
            value = argument.split("=", 1)[1].strip()
            if not value:
                raise Failure(USAGE)
            backend_override = value
        else:
            raise Failure(USAGE)
    return (
        move_mode,
        show_all,
        open_picker_mode,
        close_path,
        backend_override,
        show_tabs_override,
        start_mode_override,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["publish-state"]:
            return publish_state()
        if arguments == ["workspace-closed"]:
            return workspace_closed()
        (
            move_mode,
            show_all,
            picker,
            close_path,
            backend_override,
            show_tabs_override,
            start_mode_override,
        ) = parse(arguments)
        # Fail fast on a broken config.toml or invalid --backend/--mode, whatever the entry point.
        settings = load_settings()
        if backend_override is not None:
            from .settings import BACKENDS

            if backend_override not in BACKENDS:
                allowed = ", ".join(repr(item) for item in BACKENDS)
                raise PickerError(
                    f"Unknown backend {backend_override!r}; expected one of {allowed}"
                )
        if start_mode_override is not None and start_mode_override not in START_MODES:
            allowed = ", ".join(repr(mode) for mode in START_MODES)
            raise PickerError(
                f"Unknown start mode {start_mode_override!r}; expected one of {allowed}"
            )
        if picker:
            return open_picker(
                move_mode,
                show_all,
                backend_override,
                show_tabs_override,
                start_mode_override,
            )
        if close_path is not None:
            return close(close_path, load_names(), Herdr())
        return interactive(
            move_mode,
            show_all,
            settings,
            backend_override,
            show_tabs_override,
            start_mode_override,
        )
    except Failure as error:
        if str(error) == USAGE:
            try:
                sys.stderr.write(USAGE + "\n")
            except OSError:
                pass
            return 2
        return fail(str(error))
    except NamesError as error:
        return fail(str(error))
    except SettingsError as error:
        return fail(str(error))
    except PickerError as error:
        return fail(str(error))
    except StateError as error:
        return fail(f"Invalid workspace mapping: {error.path}")
    except HerdrError as error:
        return fail(str(error))
    except OSError as error:
        return fail(str(error))


if __name__ == "__main__":
    sys.exit(main())
