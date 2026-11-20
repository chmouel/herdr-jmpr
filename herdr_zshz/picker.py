"""Multi-backend picker frontends (fzf, skim, gum) and shared picker model."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import normalize_path, workspace_label
from .frecency import (
    CURRENT_MARK,
    OPEN_MARK,
    agents_by_path,
    current_project,
    display_path,
    format_agent_badges,
    open_paths,
    rank_paths,
    workspace_worktree_path,
)
from .herdr import Herdr, HerdrError
from .settings import BACKENDS

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

BACKEND_BINARIES = {
    "fzf": "fzf",
    "skim": "sk",
    "gum": "gum",
}

GIT_ROWS_ENV = "HERDR_ZSHZ_GIT_ROWS"
ALL_ROWS_ENV = "HERDR_ZSHZ_ALL_ROWS"
OPEN_ROWS_ENV = "HERDR_ZSHZ_OPEN_ROWS"
HELP_ENV = "HERDR_ZSHZ_HELP"
BACKEND_ENV = "HERDR_ZSHZ_BACKEND"

HELP_TEXT = (
    "enter   jump to the project (or a typed path)\n"
    "alt-p   move the current pane there\n"
    "alt-t   move the current tab there\n"
    "alt-a   switch between git roots and all dirs\n"
    "alt-o   toggle open workspaces only\n"
    "ctrl-x  close the project's workspace\n"
    "ctrl-h  toggle this help"
)

FZF_COLOR = (
    "16,bg:-1,gutter:8,bg+:0,fg:7,fg+:15,hl:4,hl+:12,prompt:4,"
    "pointer:4,input-fg:15,info:8,separator:8,spinner:8,scrollbar:8"
)

FZF_STYLE = [
    "--layout",
    "reverse",
    "--info",
    "inline-right",
    "--scrollbar",
    "--pointer",
    "▌",
    "--highlight-line",
    "--ansi",
    "--padding",
    "1,2,1,2",
    "--color",
    FZF_COLOR,
]


class PickerError(Exception):
    """Raised when a picker backend cannot run or fails."""


@dataclass
class Result:
    kind: str
    path: str
    choice: tuple[str, str | None] | None = None
    src_pane: str = ""
    src_ws: str = ""
    src_tab: str = ""
    src_layout: dict | None = None
    tab_label: str | None = None


def resolve_backend(configured: str = "textual", override: str | None = None) -> str:
    """Resolve the active picker backend from CLI, environment, or settings."""

    env_override = os.environ.get(BACKEND_ENV, "").strip()
    requested = override or env_override or configured or "textual"
    if requested not in BACKENDS:
        allowed = ", ".join(repr(item) for item in BACKENDS)
        raise PickerError(f"Unknown backend {requested!r}; expected one of {allowed}")
    if requested == "auto":
        for backend, binary_name in BACKEND_BINARIES.items():
            if shutil.which(binary_name):
                return backend
        return "textual"
    binary_name = BACKEND_BINARIES.get(requested)
    if binary_name and not shutil.which(binary_name):
        raise PickerError(
            f"{requested} backend requested, but {binary_name!r} was not found on PATH"
        )
    return requested


def header_text(move_mode: str | None, *, help_hint: bool = True) -> str:
    if move_mode == "pane":
        base = "Move pane to project workspace"
    elif move_mode == "tab":
        base = "Move tab to project workspace"
    else:
        base = "Select project workspace"
    return f"{base}  (ctrl-h: help)" if help_hint else base


def format_ansi_row(
    path: str,
    current: str | None,
    opened: set[str],
    names: dict[str, str],
    *,
    include_target: bool = True,
    agents: tuple[str, ...] = (),
) -> str:
    shown = display_path(path)
    label = workspace_label(path, names)
    base = os.path.basename(path.rstrip("/")) or path
    if label != base:
        shown += f"  \033[2m[{label}]\033[0m"
    if agents:
        badge = format_agent_badges(agents)
        shown += f"  \033[33m{badge}\033[0m"
    if path == current:
        prefix = f"\033[33m{CURRENT_MARK}\033[0m "
    elif path in opened:
        prefix = f"\033[34m{OPEN_MARK}\033[0m "
    else:
        prefix = "  "
    line = f"{prefix}{shown}"
    return f"{line}\t{path}" if include_target else line


def format_ansi_rows(
    paths: list[str],
    current: str | None,
    opened: set[str],
    names: dict[str, str],
    *,
    include_target: bool = True,
    agents: dict[str, tuple[str, ...]] | None = None,
) -> str:
    if not paths:
        return ""
    agent_map = agents or {}
    return (
        "\n".join(
            format_ansi_row(
                path,
                current,
                opened,
                names,
                include_target=include_target,
                agents=agent_map.get(path, ()),
            )
            for path in paths
        )
        + "\n"
    )


def _open_only_paths(
    paths_all: list[str], current: str | None, opened: set[str]
) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths_all:
        if (path == current or path in opened) and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def write_row_files(
    git_file: Path,
    all_file: Path,
    paths_git: list[str],
    paths_all: list[str],
    current: str | None,
    opened: set[str],
    names: dict[str, str],
    *,
    open_file: Path | None = None,
    agents: dict[str, tuple[str, ...]] | None = None,
) -> None:
    git_file.write_text(
        format_ansi_rows(paths_git, current, opened, names, agents=agents)
    )
    all_file.write_text(
        format_ansi_rows(paths_all, current, opened, names, agents=agents)
    )
    if open_file is not None:
        open_file.write_text(
            format_ansi_rows(
                _open_only_paths(paths_all, current, opened),
                current,
                opened,
                names,
                agents=agents,
            )
        )


def resolve_typed_or_picked(query: str, picked_path: str) -> str | None:
    typed = os.path.expanduser(query) if query else ""
    if typed and (query.startswith(("/", "~")) or not picked_path) and os.path.isdir(typed):
        return os.path.abspath(typed)
    return picked_path or None


def parse_query_expect_output(stdout: str) -> tuple[str, str, str]:
    """Parse ``--print-query --expect ...`` output from fzf or skim."""

    lines = stdout.splitlines()
    query = lines[0] if lines else ""
    key = ""
    picked = ""
    if len(lines) >= 3:
        key = lines[1].strip()
        picked = lines[2]
    elif len(lines) == 2:
        if "\t" in lines[1]:
            picked = lines[1]
        else:
            key = lines[1].strip()
    picked_path = picked.split("\t", 1)[1] if "\t" in picked else ""
    return query, key, picked_path


def unknown_workspaces(
    target: str,
    workspaces: list[dict],
    panes: list[dict],
    mapping: dict[str, str],
    names: dict[str, str],
) -> list[str]:
    normalized_target = normalize_path(target)
    if any(workspace_worktree_path(workspace) == normalized_target for workspace in workspaces):
        return []
    label = workspace_label(target, names)
    live = [workspace for workspace in workspaces if workspace.get("label") == label]
    if any(mapping.get(workspace.get("workspace_id")) == target for workspace in live):
        return []
    if any(
        workspace.get("workspace_id") not in mapping
        and any(
            pane.get("workspace_id") == workspace.get("workspace_id")
            and pane.get("cwd") == target
            for pane in panes
        )
        for workspace in live
    ):
        return []
    return [
        str(workspace.get("workspace_id"))
        for workspace in live
        if workspace.get("workspace_id") not in mapping
    ]


def parse_workspace_choice(
    choice_line: str, unknown_ids: list[str]
) -> tuple[str, str | None]:
    text = ANSI_RE.sub("", choice_line).strip()
    if text.startswith("Use existing workspace "):
        workspace_id = text[len("Use existing workspace ") :].strip()
        if workspace_id not in unknown_ids:
            raise PickerError(f"Invalid workspace selection: {workspace_id}")
        return ("use", workspace_id)
    if text != "Create new workspace":
        raise PickerError(f"Invalid workspace selection: {text}")
    return ("create", None)


def build_result(
    target: str,
    move_mode: str | None,
    src_pane: str,
    herdr: Herdr,
    choice: tuple[str, str | None] | None,
) -> Result:
    result = Result(kind="jump", path=target, choice=choice, src_pane=src_pane)
    if not move_mode:
        return result
    if not src_pane:
        raise PickerError("No source pane to move (HERDR_ACTIVE_PANE_ID is unset)")
    result.kind = f"move-{move_mode}"
    layout = herdr.pane_layout(src_pane)
    result.src_layout = layout
    result.src_ws = layout.get("workspace_id", "")
    result.src_tab = layout.get("tab_id", "")
    result.tab_label = "moved"
    if move_mode == "tab":
        try:
            result.tab_label = herdr.tab_label(result.src_tab) or "moved"
        except HerdrError:
            result.tab_label = "moved"
    return result


def _choose_workspace_cli(
    backend: str, target: str, unknown_ids: list[str]
) -> tuple[str, str | None] | None:
    options = ["Create new workspace"] + [
        f"Use existing workspace {workspace_id}" for workspace_id in unknown_ids
    ]
    header = f"Select workspace for {target}"
    if backend == "fzf":
        cmd = [
            shutil.which("fzf") or "fzf",
            *FZF_STYLE,
            "--header",
            header,
            "--no-sort",
        ]
    elif backend == "skim":
        cmd = [
            shutil.which("sk") or "sk",
            "--layout",
            "reverse",
            "--header",
            header,
            "--no-sort",
        ]
    elif backend == "gum":
        cmd = [
            shutil.which("gum") or "gum",
            "filter",
            "--no-fuzzy-sort",
            "--indicator",
            "▌",
            "--padding",
            "1 2",
            "--header",
            header,
        ]
    else:  # pragma: no cover - defensive
        raise PickerError(f"Unsupported CLI backend: {backend}")
    completed = subprocess.run(
        cmd,
        input="\n".join(options) + "\n",
        stdout=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        return None
    picked = completed.stdout.splitlines()
    if not picked or not picked[0].strip():
        return None
    return parse_workspace_choice(picked[0], unknown_ids)


def _finalize_cli_selection(
    backend: str,
    target: str,
    move_mode: str | None,
    src_pane: str,
    herdr: Herdr,
    mapping_reader: Callable[[], dict[str, str]],
    names: dict[str, str],
) -> Result | None:
    workspaces = herdr.workspaces()
    panes = herdr.panes()
    mapping = mapping_reader()
    unknown = unknown_workspaces(target, workspaces, panes, mapping, names)
    choice: tuple[str, str | None] | None = None
    if unknown:
        choice = _choose_workspace_cli(backend, target, unknown)
        if choice is None:
            return None
    return build_result(target, move_mode, src_pane, herdr, choice)


def _self_close_command() -> str:
    return shlex.join([sys.executable, "-m", "herdr_zshz.cli", "--close"])


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    root = str(Path(__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{root}:{existing}" if existing else root
    return os.environ | {"PYTHONPATH": pythonpath, **extra}


def run_fzf(
    *,
    paths_git: list[str],
    paths_all: list[str],
    current: str | None,
    opened: set[str],
    names: dict[str, str],
    move_mode: str | None,
    show_all: bool,
    src_pane: str,
    herdr: Herdr,
    mapping_reader: Callable[[], dict[str, str]],
    agents: dict[str, tuple[str, ...]] | None = None,
) -> Result | None:
    """Run the fzf picker."""

    binary_path = shutil.which("fzf") or "fzf"
    with tempfile.TemporaryDirectory(prefix="herdr-zshz-") as temp_dir:
        git_file = Path(temp_dir) / "git.rows"
        all_file = Path(temp_dir) / "all.rows"
        open_file = Path(temp_dir) / "open.rows"
        write_row_files(
            git_file,
            all_file,
            paths_git,
            paths_all,
            current,
            opened,
            names,
            open_file=open_file,
            agents=agents,
        )
        rows_file = all_file if show_all else git_file
        prompt = "all> " if show_all else "git> "
        toggle = (
            'if [ "$FZF_PROMPT" = "all> " ]; then '
            f'echo \'reload(cat "${GIT_ROWS_ENV}")+change-prompt(git> )\'; '
            "else "
            f'echo \'reload(cat "${ALL_ROWS_ENV}")+change-prompt(all> )\'; '
            "fi"
        )
        toggle_open = (
            'if [ "$FZF_PROMPT" = "open> " ]; then '
            f'echo \'reload(cat "${GIT_ROWS_ENV}")+change-prompt(git> )\'; '
            "else "
            f'echo \'reload(cat "${OPEN_ROWS_ENV}")+change-prompt(open> )\'; '
            "fi"
        )
        refresh = (
            'if [ "$FZF_PROMPT" = "all> " ]; then '
            f'echo \'reload(cat "${ALL_ROWS_ENV}")\'; '
            'elif [ "$FZF_PROMPT" = "open> " ]; then '
            f'echo \'reload(cat "${OPEN_ROWS_ENV}")\'; '
            "else "
            f'echo \'reload(cat "${GIT_ROWS_ENV}")\'; '
            "fi"
        )
        close_cmd = _self_close_command()
        args = [
            binary_path,
            *FZF_STYLE,
            "--header",
            header_text(move_mode, help_hint=True),
            "--prompt",
            prompt,
            "--bind",
            f"alt-a:transform:{toggle}",
            "--bind",
            f"alt-o:transform:{toggle_open}",
            "--bind",
            f"ctrl-x:execute({close_cmd} {{2}})+transform:{refresh}",
            "--preview",
            f'printf "%s\\n" "${HELP_ENV}"',
            "--preview-window",
            "hidden,down,7,border-top",
            "--bind",
            "ctrl-h:toggle-preview",
            "--delimiter",
            "\t",
            "--with-nth",
            "1",
            "--print-query",
            "--expect",
            "alt-p,alt-t",
            "--no-sort",
            "-e",
        ]
        env = _child_env(
            {
                GIT_ROWS_ENV: str(git_file),
                ALL_ROWS_ENV: str(all_file),
                OPEN_ROWS_ENV: str(open_file),
                HELP_ENV: HELP_TEXT,
            }
        )
        with open(rows_file) as stdin_handle:
            completed = subprocess.run(
                args,
                stdin=stdin_handle,
                stdout=subprocess.PIPE,
                text=True,
                env=env,
            )

    # 1 means nothing matched the query, which may still be a typed directory path.
    if completed.returncode not in (0, 1):
        return None
    query, key, picked_path = parse_query_expect_output(completed.stdout)
    selection = resolve_typed_or_picked(query, picked_path)
    if not selection:
        return None
    effective_move = move_mode
    if key == "alt-p":
        effective_move = "pane"
    elif key == "alt-t":
        effective_move = "tab"
    return _finalize_cli_selection(
        "fzf", selection, effective_move, src_pane, herdr, mapping_reader, names
    )


def run_skim(
    *,
    paths_git: list[str],
    paths_all: list[str],
    current: str | None,
    opened: set[str],
    names: dict[str, str],
    move_mode: str | None,
    show_all: bool,
    src_pane: str,
    herdr: Herdr,
    mapping_reader: Callable[[], dict[str, str]],
    history: list[str],
    close_callback: Callable[[str], int],
    agents: dict[str, tuple[str, ...]] | None = None,
) -> Result | None:
    """Run the skim (``sk``) picker with Python-driven toggle and close actions."""

    binary_path = shutil.which("sk") or "sk"
    scope = "all" if show_all else "git"
    previous_scope = scope
    cur_project = current
    cur_opened = set(opened)
    cur_agents = dict(agents) if agents else {}
    git_list = list(paths_git)
    all_list = list(paths_all)
    initial_query = ""

    while True:
        if scope == "open":
            active_paths = _open_only_paths(all_list, cur_project, cur_opened)
        elif scope == "all":
            active_paths = all_list
        else:
            active_paths = git_list
        rows = format_ansi_rows(
            active_paths, cur_project, cur_opened, names, agents=cur_agents
        )
        prompt = f"{scope}> "
        args = [
            binary_path,
            "--layout",
            "reverse",
            "--inline-info",
            "--ansi",
            "--header",
            header_text(move_mode, help_hint=True),
            "--prompt",
            prompt,
            "--preview",
            f'printf "%s\\n" "${HELP_ENV}"',
            "--preview-window",
            "down:7:hidden",
            "--bind",
            "ctrl-h:toggle-preview",
            "--delimiter",
            "\t",
            "--with-nth",
            "1",
            "--print-query",
            "--expect",
            "alt-p,alt-t,alt-a,alt-o,ctrl-x",
            "--no-sort",
            "-e",
        ]
        if initial_query:
            args.extend(["--query", initial_query])
        completed = subprocess.run(
            args,
            input=rows,
            stdout=subprocess.PIPE,
            text=True,
            env=_child_env({HELP_ENV: HELP_TEXT}),
        )
        if completed.returncode not in (0, 1):
            return None
        query, key, picked_path = parse_query_expect_output(completed.stdout)
        if key == "alt-a":
            scope = "git" if scope == "all" else "all"
            previous_scope = scope
            initial_query = query
            continue
        if key == "alt-o":
            if scope == "open":
                scope = previous_scope
            else:
                previous_scope = scope
                scope = "open"
            initial_query = query
            continue
        if key == "ctrl-x":
            if picked_path:
                close_callback(picked_path)
                workspaces = herdr.workspaces()
                panes = herdr.panes()
                mapping = mapping_reader()
                cur_opened = open_paths(workspaces, panes, mapping, names)
                cur_project = current_project(src_pane, panes, workspaces, mapping, names)
                cur_agents = agents_by_path(workspaces, panes, mapping, names)
                git_list = rank_paths(history, cur_project, cur_opened, False)
                all_list = rank_paths(history, cur_project, cur_opened, True)
            initial_query = query
            continue

        selection = resolve_typed_or_picked(query, picked_path)
        if not selection:
            return None
        effective_move = move_mode
        if key == "alt-p":
            effective_move = "pane"
        elif key == "alt-t":
            effective_move = "tab"
        return _finalize_cli_selection(
            "skim", selection, effective_move, src_pane, herdr, mapping_reader, names
        )


def run_gum(
    *,
    paths_git: list[str],
    paths_all: list[str],
    current: str | None,
    opened: set[str],
    names: dict[str, str],
    move_mode: str | None,
    show_all: bool,
    src_pane: str,
    herdr: Herdr,
    mapping_reader: Callable[[], dict[str, str]],
    agents: dict[str, tuple[str, ...]] | None = None,
) -> Result | None:
    """Run ``gum filter`` with ANSI labels and typed-directory support."""

    binary_path = shutil.which("gum") or "gum"
    paths = paths_all if show_all else paths_git
    agent_map = agents or {}
    by_raw: dict[str, str] = {}
    by_plain: dict[str, str] = {}
    lines: list[str] = []
    for path in paths:
        line = format_ansi_row(
            path,
            current,
            opened,
            names,
            include_target=False,
            agents=agent_map.get(path, ()),
        )
        lines.append(line)
        by_raw[line] = path
        by_plain[ANSI_RE.sub("", line)] = path

    prompt = "all> " if show_all else "git> "
    args = [
        binary_path,
        "filter",
        "--no-strip-ansi",
        "--no-fuzzy-sort",
        "--no-strict",
        "--indicator",
        "▌",
        "--padding",
        "1 2",
        "--prompt",
        prompt,
        "--header",
        header_text(move_mode, help_hint=False),
        "--placeholder",
        "Filter workspaces or type a path…",
    ]
    completed = subprocess.run(
        args,
        input="\n".join(lines) + ("\n" if lines else ""),
        stdout=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        return None
    picked = completed.stdout.rstrip("\r\n")
    if not picked:
        return None
    selection = by_raw.get(picked) or by_plain.get(ANSI_RE.sub("", picked))
    if not selection:
        selection = resolve_typed_or_picked(picked, "")
    if not selection:
        return None
    return _finalize_cli_selection(
        "gum", selection, move_mode, src_pane, herdr, mapping_reader, names
    )
