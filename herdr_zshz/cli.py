"""Command line entry point for the herdr-zshz plugin."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import NamesError, load_names, workspace_label
from .frecency import current_project, open_paths, rank_paths, read_history
from .herdr import Herdr, HerdrError, layout_ops
from .state import (
    StateError,
    lock_file,
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
from .tui import PickerApp

USAGE = "usage: herdr-zshz [--move-pane|--move-tab] [--all]"
PLUGIN_ID = "herdr-zshz"


class Failure(Exception):
    pass


def active_pane_id() -> str:
    for variable in ("HERDR_ACTIVE_PANE_ID", "HERDR_PANE_ID"):
        value = os.environ.get(variable)
        if value:
            return value
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if not raw:
        return ""
    try:
        context = json.loads(raw)
    except ValueError:
        return ""
    for key in ("focused_pane", "pane"):
        value = context.get(key)
        if isinstance(value, dict) and value.get("pane_id"):
            return str(value["pane_id"])
    pane_id = context.get("pane_id")
    return str(pane_id) if pane_id else ""


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


def open_picker(move_mode: str | None, show_all: bool) -> int:
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
    if pane_id:
        command += ["--env", f"HERDR_ACTIVE_PANE_ID={pane_id}"]
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

    ops = layout_ops(result.src_layout or {})
    tab_label = result.tab_label or "moved"
    anchor = ops[0]
    if target:
        pane = herdr.pane_move(
            anchor, "--new-tab", "--workspace", target, "--label", tab_label
        )
    else:
        pane = herdr.pane_move(
            anchor, "--new-workspace", "--label", label, "--tab-label", tab_label
        )
    moved: dict[str, str] = {anchor: pane.get("pane_id", "")}
    new_tab = pane.get("tab_id", "")
    for operation in ops[1:]:
        source, target_pane, direction, ratio = operation.split("\t")
        pane = herdr.pane_move(
            source,
            "--tab",
            new_tab,
            "--split",
            direction,
            "--target-pane",
            moved[target_pane],
            "--ratio",
            ratio,
        )
        moved[source] = pane.get("pane_id", "")
    return pane.get("workspace_id", ""), new_tab


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
        workspaces = herdr.workspaces()
        panes = herdr.panes()
        live_ids = {str(workspace.get("workspace_id")) for workspace in workspaces}
        state = recover(read_mapping(path), socket, instance, live_ids)
        destination, tab_id = resolve_and_move(
            result, target, label, state, workspaces, panes, herdr
        )
        if result.kind != "jump":
            tab_id = ""
            if destination != result.src_ws:
                destination, tab_id = move_source(result, destination, label, herdr)
        state["workspaces"][destination] = target
        write_state(path, state)
    except BaseException:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
        raise
    fcntl.flock(handle, fcntl.LOCK_UN)
    os.close(handle)
    herdr.workspace_focus(destination)
    if tab_id:
        herdr.tab_focus(tab_id)
    return 0


def interactive(move_mode: str | None, show_all: bool) -> int:
    names = load_names()
    herdr = Herdr()
    socket = socket_path(herdr)
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = state_file(socket)
    instance = socket_instance(socket)
    workspaces = herdr.workspaces()
    panes = herdr.panes()
    state = read_mapping(path)
    mapping = (
        dict(state["workspaces"]) if state.get("instance") == instance else {}
    )
    pane_id = active_pane_id()
    opened = open_paths(workspaces, panes, mapping, names)
    current = current_project(pane_id, panes, workspaces, mapping, names)
    history = read_history()
    paths_git = rank_paths(history, current, opened, False)
    paths_all = rank_paths(history, current, opened, True)
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
        mapping_reader=lambda: read_live_mapping(path, instance),
        strict_mapping_reader=lambda: read_live_mapping_strict(path, instance),
        history=history,
    )
    result = app.run()
    ignore_hangup()
    if result is None:
        return 0
    return jump(result, herdr, socket, instance, path, names)


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
    mapping = read_live_mapping_strict(path, instance)
    workspaces = herdr.workspaces()
    panes = herdr.panes()
    close_ids, agent_tuples, agent_names = snapshot_close_targets(
        target, workspaces, panes, mapping, names
    )
    if not close_ids:
        return 0
    approved_ids = set(close_ids)
    if agent_tuples and not confirm(
        f"Close {label}? {', '.join(agent_names)} running there [y/N] "
    ):
        return 0
    workspaces = herdr.workspaces()
    panes = herdr.panes()
    mapping = read_live_mapping_strict(path, instance)
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
    return 0


def parse(argv: list[str]) -> tuple[str | None, bool, bool, str]:
    move_mode: str | None = None
    show_all = False
    open_picker_mode = False
    close_path: str | None = None
    remaining = list(argv)
    while remaining:
        argument = remaining.pop(0)
        if argument == "--move-pane":
            move_mode = "pane"
        elif argument == "--move-tab":
            move_mode = "tab"
        elif argument == "--all":
            show_all = True
        elif argument == "open-picker":
            open_picker_mode = True
        elif argument == "--close":
            close_path = remaining.pop(0) if remaining else ""
        else:
            raise Failure(USAGE)
    return move_mode, show_all, open_picker_mode, close_path


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        move_mode, show_all, picker, close_path = parse(arguments)
        if picker:
            return open_picker(move_mode, show_all)
        if close_path is not None:
            return close(close_path, load_names(), Herdr())
        return interactive(move_mode, show_all)
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
    except StateError as error:
        return fail(f"Invalid workspace mapping: {error.path}")
    except HerdrError as error:
        return fail(str(error))
    except OSError as error:
        return fail(str(error))


if __name__ == "__main__":
    sys.exit(main())
