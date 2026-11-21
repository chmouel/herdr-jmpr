#!/usr/bin/env python3
"""Render the README screenshots of the Textual picker.

Usage: uv run python scripts/screenshots.py [--out docs/screenshots]

The script builds a throwaway $HOME with a handful of git repositories and
fake Herdr workspaces, so no real paths or session data end up in the images.
It always writes SVG; it also writes PNG when ``rsvg-convert`` is on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOS = {
    "api-gateway": "Add rate limiting to auth middleware",
    "web-console": "Fix login redirect loop",
    "infra": "Bump cluster module to v4",
    "docs-site": "Document the tab tree",
    "dotfiles": "Tune tmux status line",
}


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=demo", "-c", "user.email=demo@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def build_home(root: Path) -> Path:
    home = root / "home"
    for name, subject in REPOS.items():
        repo = home / "src" / name
        repo.mkdir(parents=True)
        git("init", "-q", cwd=repo)
        git("commit", "-q", "--allow-empty", "-m", subject, cwd=repo)
    (home / "src" / "api-gateway" / "auth.py").write_text("")
    return home


def demo_state(home: Path) -> dict:
    from herdr_jmpr.frecency import (
        agents_by_path,
        current_project,
        open_paths,
        rank_paths,
        tabs_by_path,
    )

    src = home / "src"
    api, web, infra, docs, dotfiles = (
        str(src / name) for name in ("api-gateway", "web-console", "infra", "docs-site", "dotfiles")
    )
    workspaces = [
        {"workspace_id": "w1", "label": "api-gateway", "active_tab_id": "w1:t1"},
        {"workspace_id": "w2", "label": "web-console", "active_tab_id": "w2:t1"},
        {"workspace_id": "w3", "label": "infra", "active_tab_id": "w3:t1"},
    ]
    raw_tabs = [
        {"workspace_id": "w1", "tab_id": "w1:t1", "label": "Review auth middleware",
         "number": 1, "focused": False, "agent_status": "working"},
        {"workspace_id": "w1", "tab_id": "w1:t2", "label": "tests",
         "number": 2, "focused": False, "agent_status": "unknown"},
        {"workspace_id": "w1", "tab_id": "w1:t3", "label": "server",
         "number": 3, "focused": False, "agent_status": "unknown"},
        {"workspace_id": "w2", "tab_id": "w2:t1", "label": "Fix login redirect",
         "number": 1, "focused": False, "agent_status": "idle"},
        {"workspace_id": "w2", "tab_id": "w2:t2", "label": "lazygit",
         "number": 2, "focused": False, "agent_status": "unknown"},
        {"workspace_id": "w3", "tab_id": "w3:t1", "label": "shell",
         "number": 1, "focused": False, "agent_status": "unknown"},
        {"workspace_id": "w3", "tab_id": "w3:t2", "label": "notes",
         "number": 2, "focused": True, "agent_status": "unknown"},
    ]
    panes = [
        {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1", "cwd": api,
         "agent": "pi", "agent_status": "working", "tokens": {"tl": "Review auth middleware"}},
        {"workspace_id": "w1", "tab_id": "w1:t2", "pane_id": "w1:p2", "cwd": api,
         "agent": None, "agent_status": "unknown"},
        {"workspace_id": "w1", "tab_id": "w1:t3", "pane_id": "w1:p3", "cwd": api,
         "agent": None, "agent_status": "unknown"},
        {"workspace_id": "w2", "tab_id": "w2:t1", "pane_id": "w2:p1", "cwd": web,
         "agent": "claude", "agent_status": "idle", "tokens": {"tl": "Fix login redirect"}},
        {"workspace_id": "w2", "tab_id": "w2:t2", "pane_id": "w2:p2", "cwd": web,
         "agent": None, "agent_status": "unknown"},
        {"workspace_id": "w3", "tab_id": "w3:t1", "pane_id": "w3:p1", "cwd": infra,
         "agent": None, "agent_status": "unknown"},
        {"workspace_id": "w3", "tab_id": "w3:t2", "pane_id": "w3:p2", "cwd": docs,
         "agent": None, "agent_status": "unknown", "focused": True},
    ]
    mapping = {"w1": api, "w2": web, "w3": infra}
    names: dict[str, str] = {}
    history = [api, web, infra, docs, dotfiles]
    opened = open_paths(workspaces, panes, mapping, names)
    current = current_project("w3:p2", panes, workspaces, mapping, names)
    return {
        "workspaces": workspaces,
        "panes": panes,
        "raw_tabs": raw_tabs,
        "mapping": mapping,
        "names": names,
        "opened": opened,
        "current": current,
        "agents": agents_by_path(workspaces, panes, mapping, names),
        "tabs": tabs_by_path(workspaces, raw_tabs, panes, mapping, names),
        "paths_git": rank_paths(history, current, opened, False),
        "paths_all": rank_paths(history, current, opened, True),
    }


def make_app(state: dict, *, start_mode: str = "insert", theme: str = "herdr-ink"):
    from herdr_jmpr.herdr import Herdr
    from herdr_jmpr.tui import PickerApp

    return PickerApp(
        paths_git=state["paths_git"],
        paths_all=state["paths_all"],
        current=state["current"],
        opened=state["opened"],
        names=state["names"],
        herdr=Herdr("/bin/false"),
        mapping_reader=lambda: state["mapping"],
        src_pane="w3:p2",
        history=state["paths_all"],
        start_mode=start_mode,
        theme_setting=theme,
        workspaces=state["workspaces"],
        panes=state["panes"],
        agents=state["agents"],
        tabs=state["tabs"],
        show_tabs=True,
        show_preview=True,
    )


def row_index(app, tab_id: str | None = None, path: str | None = None) -> int:
    for index, row in enumerate(app.filtered):
        if tab_id is not None and row.tab_id == tab_id:
            return index
        if tab_id is None and path is not None and not row.is_tab and row.path == path:
            return index
    raise LookupError(tab_id or path)


Scene = Callable[[object, object, dict], Awaitable[None]]


async def tree_scene(app, pilot, state) -> None:
    app.set_index(row_index(app, tab_id="w1:t1"))


async def search_scene(app, pilot, state) -> None:
    app.insert_text("web")


async def yank_scene(app, pilot, state) -> None:
    app.set_index(row_index(app, tab_id="w1:t2"))
    app.yank_selected()
    app.set_index(row_index(app, tab_id="w2:t2"))
    app.yank_selected()
    app.set_index(row_index(app, tab_id="w3:t1"))
    await pilot.pause(0.1)


async def dracula_scene(app, pilot, state) -> None:
    app.set_index(row_index(app, tab_id="w1:t1"))


SCENES: list[tuple[str, dict, Scene]] = [
    ("picker-tree", {}, tree_scene),
    ("picker-search", {}, search_scene),
    ("picker-yank", {"start_mode": "normal"}, yank_scene),
    ("picker-dracula", {"theme": "dracula"}, dracula_scene),
]


async def render(state: dict, options: dict, scene: Scene) -> str:
    app = make_app(state, **options)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause(0.2)
        await scene(app, pilot, state)
        await pilot.pause(0.4)
        return app.export_screenshot(title="herdr-jmpr")


def to_png(svg: Path, png: Path) -> None:
    subprocess.run(
        ["rsvg-convert", "--width", "1600", "--output", str(png), str(svg)],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    args.out.mkdir(parents=True, exist_ok=True)
    have_rsvg = shutil.which("rsvg-convert") is not None

    with tempfile.TemporaryDirectory(prefix="herdr-jmpr-shots-") as tmp:
        home = build_home(Path(tmp))
        os.environ["HOME"] = str(home)
        os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
        state = demo_state(home)
        for name, options, scene in SCENES:
            svg = asyncio.run(render(state, options, scene))
            svg_path = args.out / f"{name}.svg"
            svg_path.write_text(svg)
            print(f"wrote {svg_path.relative_to(ROOT)}")
            if have_rsvg:
                png_path = args.out / f"{name}.png"
                to_png(svg_path, png_path)
                print(f"wrote {png_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
