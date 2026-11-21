import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.cells import cell_len
from textual.app import App
from textual.worker import NoActiveWorker, get_current_worker

from herdr_jmpr.frecency import TabEntry, agent_icon, format_agent_badges, rank_paths
from herdr_jmpr.settings import SettingsError
from herdr_jmpr.state import StateError
from herdr_jmpr.tui import (
    MAX_CLOSE_ROUNDS,
    CommitInfo,
    ConfirmCloseModal,
    GitInfo,
    HelpModal,
    PickerApp,
    PreviewData,
    RowList,
    SelectWorkspaceModal,
    SessionInfo,
    ThemePickerModal,
    WorkspaceInfo,
    _parse_git_log,
    _parse_git_status_v2,
)


def visible(app):
    """Rows a user can select: the filtered list without group dividers."""
    return [row for row in app.filtered if not row.is_divider]


def layout(workspace_id, pane_ids):
    panes, splits, x, width = [], [], 0, 100
    for index, pane_id in enumerate(pane_ids):
        if index < len(pane_ids) - 1:
            splits.append(
                {
                    "direction": "right",
                    "ratio": 0.6,
                    "rect": {"x": x, "y": 0, "width": width, "height": 50},
                }
            )
            left = width * 6 // 10
            panes.append(
                {"pane_id": pane_id, "rect": {"x": x, "y": 0, "width": left, "height": 50}}
            )
            x, width = x + left, width - left
        else:
            panes.append(
                {"pane_id": pane_id, "rect": {"x": x, "y": 0, "width": width, "height": 50}}
            )
    return {
        "area": {"x": 0, "y": 0, "width": 100, "height": 50},
        "panes": panes,
        "splits": splits,
        "tab_id": "t-src",
        "workspace_id": "src",
    }


class FakeHerdr:
    def __init__(self, workspaces=None, panes=None, tab="mytab"):
        self._workspaces = list(workspaces or [])
        self._panes = list(panes or [])
        self.tab = tab
        self.closed: list[str] = []
        self.layout = layout("src", ["A", "B"])

    def workspaces(self):
        return self._workspaces

    def panes(self):
        return self._panes

    def pane_layout(self, pane_id):
        return self.layout

    def tab_label(self, tab_id):
        return self.tab

    def snapshot(self):
        tabs_fn = getattr(self, "tabs", None)
        tabs = list(tabs_fn()) if callable(tabs_fn) else []
        if not tabs:
            tabs = [
                {
                    "tab_id": self.layout.get("tab_id"),
                    "workspace_id": self.layout.get("workspace_id"),
                    "label": self.tab,
                }
            ]
        panes = self.panes()
        layouts, seen = [], set()
        for pane in panes:
            tab_id = pane.get("tab_id")
            if not tab_id or tab_id in seen or not pane.get("pane_id"):
                continue
            seen.add(tab_id)
            layouts.append(self.pane_layout(pane["pane_id"]))
        if not layouts:
            layouts = [self.layout]
        return {
            "workspaces": self.workspaces(),
            "panes": panes,
            "tabs": tabs,
            "layouts": layouts,
        }

    def workspace_close(self, workspace_id):
        self.closed.append(workspace_id)
        self._workspaces = [
            workspace
            for workspace in self._workspaces
            if workspace["workspace_id"] != workspace_id
        ]
        self._panes = [
            pane for pane in self._panes if pane.get("workspace_id") != workspace_id
        ]


class PickerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = str(self.root / "repo")
        self.other = str(self.root / "other")
        os.makedirs(self.repo)
        os.makedirs(self.other)
        self.herdr = FakeHerdr()
        self.mapping: dict[str, str] = {}

    def app(self, **kwargs):
        options = {
            "paths_git": [self.repo, self.other],
            "paths_all": [self.repo, self.other],
            "current": None,
            "opened": set(),
            "names": {},
            "herdr": self.herdr,
            "mapping_reader": lambda *_: self.mapping,
            "src_pane": "A",
        }
        options.update(kwargs)
        return PickerApp(**options)

    async def test_typing_filters_rows(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("r", "e", "p", "o")
            self.assertEqual(app.query, "repo")
            self.assertEqual([row.path for row in visible(app)], [self.repo])
            await pilot.press("escape")
            self.assertEqual(app.mode, "normal")
            await pilot.press("g", "g")
            self.assertEqual(app.index, 0)
            await pilot.press("G")
            self.assertEqual(app.index, 0)
            await pilot.press("d", "d")
            self.assertEqual(app.query, "")
            self.assertEqual(len(visible(app)), 2)

    async def test_start_mode_insert_is_the_default(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.mode, "insert")

    async def test_start_mode_normal_from_settings(self):
        app = self.app(start_mode="normal")
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.mode, "normal")
            # Typing must navigate, not filter, until an insert key is pressed.
            await pilot.press("j")
            self.assertEqual(app.index, 1)
            self.assertEqual(app.query, "")
            await pilot.press("i")
            self.assertEqual(app.mode, "insert")
            await pilot.press("r", "e", "p")
            self.assertEqual(app.query, "rep")
            self.assertEqual([row.path for row in visible(app)], [self.repo])

    async def test_enter_returns_selection(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsNotNone(app.return_value)
        self.assertEqual(app.return_value.path, self.repo)
        self.assertEqual(app.return_value.kind, "jump")

    async def test_ctrl_n_and_arrows_move(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+n")
            self.assertEqual(app.index, 1)
            await pilot.press("up")
            self.assertEqual(app.index, 0)

    async def test_paging_and_viewport_moves(self):
        paths = [str(self.root / f"p{index:03}") for index in range(60)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("page_down")
            self.assertGreater(app.index, 0)
            await pilot.press("escape")
            await pilot.press("G")
            self.assertEqual(app.index, len(paths) - 1)
            container = app.row_list
            top = int(container.scroll_offset.y)
            self.assertGreater(top, 0)
            await pilot.press("H")
            self.assertEqual(app.index, top)
            top = int(container.scroll_offset.y)
            await pilot.press("L")
            self.assertEqual(
                app.index, min(len(paths) - 1, top + container.size.height - 1)
            )
            await pilot.press("M")
            top = int(container.scroll_offset.y)
            bottom = min(len(paths) - 1, top + container.size.height - 1)
            self.assertEqual(app.index, (top + bottom) // 2)
            await pilot.press("g", "g")
            self.assertEqual(app.index, 0)
            await pilot.press("ctrl+d")
            self.assertGreater(app.index, 0)

    async def test_viewport_keys_follow_a_scrolled_list(self):
        paths = [str(self.root / f"s{index:03}") for index in range(30)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(100, 12)) as pilot:
            await pilot.pause()
            container = app.row_list
            container.scroll_to(y=7, animate=False)
            await pilot.pause()
            self.assertEqual(int(container.scroll_offset.y), 7)
            await pilot.press("escape")
            await pilot.press("H")
            self.assertEqual(app.index, 7)
            self.assertNotEqual(app.index, 0)

    async def test_first_preview_loads_at_once_and_prefetches_the_next_row(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertIsNone(app._preview_timer)
            for _ in range(200):
                if self.other in app._preview_cache:
                    break
                await pilot.pause(0.02)
            self.assertIn(self.repo, app._preview_cache)
            self.assertIn(self.other, app._preview_cache)
            await pilot.press("down")
            self.assertFalse(app.preview_data.loading)
            self.assertEqual(app.preview_data.path, self.other)

    async def test_click_on_a_scrolled_list_selects_the_row_under_the_pointer(self):
        paths = [str(self.root / f"m{index:03}") for index in range(30)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(100, 12)) as pilot:
            await pilot.pause()
            app.row_list.scroll_to(y=10, animate=False)
            await pilot.pause()
            await pilot.click("#list", offset=(4, 2))
            self.assertEqual(app.index, 12)

    async def test_only_visible_and_changed_rows_are_rendered(self):
        paths = [str(self.root / f"v{index:04}") for index in range(500)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            height = app.row_list.size.height
            with mock.patch.object(
                RowList, "_row_text", autospec=True, side_effect=RowList._row_text
            ) as row_text:
                await pilot.press("v")
                await pilot.pause()
                self.assertLessEqual(row_text.call_count, height)
                row_text.reset_mock()
                await pilot.press("down")
                await pilot.pause()
                self.assertEqual(
                    sorted(call.args[1] for call in row_text.call_args_list), [0, 1]
                )

    async def test_cursor_moves_without_scrolling_the_list(self):
        paths = [str(self.root / f"c{index:03}") for index in range(40)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            container = app.row_list
            height = container.size.height
            self.assertGreater(height, 3)
            # Inside the viewport the list must stay still, and the container
            # must not scroll on its own as it did when it could take focus.
            for expected in range(1, height):
                await pilot.press("down")
                self.assertEqual(app.index, expected)
                self.assertEqual(int(container.scroll_offset.y), 0)
            # Crossing the bottom edge scrolls by exactly one line.
            await pilot.press("down")
            self.assertEqual(int(container.scroll_offset.y), 1)
            for step in range(2, 6):
                await pilot.press("down")
                self.assertEqual(int(container.scroll_offset.y), step)
            # Going back up inside the viewport leaves the offset alone, and only
            # scrolls again once the cursor passes the top edge.
            await pilot.press("up")
            self.assertEqual(int(container.scroll_offset.y), 5)
            top = int(container.scroll_offset.y)
            while app.index > top:
                await pilot.press("up")
            self.assertEqual(int(container.scroll_offset.y), top)
            await pilot.press("up")
            self.assertEqual(int(container.scroll_offset.y), top - 1)

    async def test_arrow_keys_are_not_handled_twice(self):
        paths = [str(self.root / f"k{index:03}") for index in range(40)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            self.assertFalse(app.row_list.can_focus)
            await pilot.press("down")
            self.assertEqual(app.index, 1)
            await pilot.press("ctrl+n")
            self.assertEqual(app.index, 2)

    async def test_selected_row_is_visibly_highlighted(self):
        app = self.app()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            text = app.row_list._build_text()
            lines = text.split("\n")
            self.assertTrue(str(lines[1]).startswith("\u258c"))
            self.assertFalse(str(lines[0]).startswith("\u258c"))
            style = app.row_list.selected_row_style
            self.assertNotEqual(style, "bold reverse")
            self.assertTrue(style.startswith("on #"), style)
            highlighted = [
                span
                for span in text.spans
                if span.start == len(lines[0]) + 1 and str(span.style).startswith("on #")
            ]
            self.assertTrue(highlighted, [str(span.style) for span in text.spans])

    async def test_selection_style_falls_back_when_the_theme_has_no_colour(self):
        app = self.app()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            theme = app.current_theme
            with mock.patch.object(
                type(theme), "primary", new_callable=mock.PropertyMock, create=True
            ) as primary:
                primary.return_value = "ansi_blue"
                self.assertEqual(app.selection_style(), "bold reverse")

    async def test_long_paths_are_truncated_instead_of_wrapped(self):
        paths = [str(self.root / ("long-name-" * 14 + f"{index}")) for index in range(6)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(70, 16)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            text = app.row_list._build_text()
            lines = str(text).split("\n")
            # One screen line per row: wrapping would shift every later row and
            # hide the cursor.
            self.assertEqual(len([line for line in lines if line]), len(paths))
            for line in lines:
                self.assertLessEqual(len(line), 70)
            self.assertIn("\u2026", lines[1])
            self.assertTrue(lines[1].rstrip().endswith("long-name-1"), lines[1])

    async def test_selection_bar_spans_the_whole_column(self):
        paths = [str(self.root / ("wide-row-" * 20 + f"{index}")) for index in range(4)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(70, 16)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            col_width = app.row_list.scrollable_content_region.width
            text = app.row_list._build_text()
            lines = str(text).split("\n")
            self.assertEqual(len(lines[0]), col_width)
            self.assertEqual(len(lines[1]), col_width)
            start = len(lines[0]) + 1
            padded = [
                span
                for span in text.spans
                if span.start <= start
                and span.end >= start + col_width
                and str(span.style).startswith("on ")
            ]
            self.assertTrue(padded, [(s.start, s.end, str(s.style)) for s in text.spans])

    async def test_toggle_all(self):
        app = self.app(paths_all=[self.repo, self.other, str(self.root)])
        async with app.run_test() as pilot:
            await pilot.press("alt+a")
            self.assertTrue(app.show_all)
            self.assertEqual(len(app.rows), 3)

    async def test_tab_completes_path(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("tab")
            self.assertEqual(app.query, self.repo)

    async def test_move_mode_returns_layout(self):
        app = self.app(move_mode="tab")
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(app.return_value.kind, "move-tab")
        self.assertEqual(app.return_value.src_ws, "src")
        self.assertEqual(app.return_value.tab_label, "mytab")
        self.assertEqual(app.return_value.src_layout, self.herdr.layout)

    async def test_typed_path_wins(self):
        app = self.app()
        async with app.run_test() as pilot:
            app.insert_text(self.other + "/")
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsNotNone(app.return_value)
        self.assertEqual(app.return_value.path, os.path.abspath(self.other))

    async def test_preview_shown_on_wide_terminal(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            preview = app.query_one("#preview")
            self.assertTrue(preview.display)
            self.assertIsNotNone(app.preview_data)
            self.assertEqual(app.preview_data.path, self.repo)


class CloseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = str(self.root / "repo")
        os.makedirs(self.repo)
        self.mapping = {"w1": self.repo}

    def app(self, herdr):
        return PickerApp(
            paths_git=[self.repo],
            paths_all=[self.repo],
            current=None,
            opened={self.repo},
            names={},
            herdr=herdr,
            mapping_reader=lambda *_: self.mapping,
            src_pane="A",
        )

    async def test_close_without_agent_closes_directly(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w1", "label": "repo", "active_tab_id": "t"}],
            panes=[{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t", "cwd": self.repo}],
        )
        app = self.app(herdr)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmCloseModal)
            self.assertEqual(herdr.closed, ["w1"])

    async def test_close_asks_when_agent_runs_there(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w1", "label": "repo", "active_tab_id": "t"}],
            panes=[
                {
                    "workspace_id": "w1",
                    "pane_id": "p1",
                    "tab_id": "t",
                    "cwd": self.repo,
                    "agent": "copilot",
                }
            ],
        )
        app = self.app(herdr)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmCloseModal)
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(herdr.closed, [])
            await pilot.press("ctrl+x")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(herdr.closed, ["w1"])

    async def test_close_renotifies_when_agent_appears(self):
        self.mapping = {"w1": self.repo}
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w1", "label": "repo", "active_tab_id": "t"}],
            panes=[{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t", "cwd": self.repo}],
        )
        original = herdr.panes

        calls = {"count": 0}

        def panes():
            calls["count"] += 1
            if calls["count"] >= 2:
                return [
                    {
                        "workspace_id": "w1",
                        "pane_id": "p2",
                        "tab_id": "t",
                        "cwd": self.repo,
                        "agent": "copilot",
                    }
                ]
            return original()

        herdr.panes = panes
        app = self.app(herdr)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmCloseModal)
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(herdr.closed, ["w1"])

    async def wait_for_preview(self, app, pilot):
        for _ in range(200):
            if app.preview_data is not None and not app.preview_data.loading:
                return True
            await pilot.pause(0.02)
        return False

    async def test_preview_details_stay_when_the_selection_does_not_change(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w1", "label": "repo", "active_tab_id": "t"}],
            panes=[{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t", "cwd": self.repo}],
        )
        app = self.app(herdr)
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertTrue(await self.wait_for_preview(app, pilot))
            self.assertEqual([w.workspace_id for w in app.preview_data.workspaces], ["w1"])
            self.assertEqual(app.preview_data.label, "repo")
            app.insert_text("rep")
            await pilot.pause()
            self.assertEqual([row.path for row in visible(app)], [self.repo])
            self.assertEqual([w.workspace_id for w in app.preview_data.workspaces], ["w1"])
            self.assertEqual(app.preview_data.workspaces[0].tabs, ("t",))
            app.clear_query()
            await pilot.pause()
            self.assertEqual([w.workspace_id for w in app.preview_data.workspaces], ["w1"])

    async def test_preview_from_before_invalidation_is_not_cached(self):
        herdr = FakeHerdr(workspaces=[], panes=[])
        app = self.app(herdr)
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertTrue(await self.wait_for_preview(app, pilot))
            app.invalidate_previews()
            stale_generation = app._preview_generation - 1
            stale_data = PreviewData(path=self.repo, label="stale")
            fresh_data = PreviewData(path=self.repo, label="fresh")
            app._preview_loaded(self.repo, stale_data, stale_generation)
            await pilot.pause()
            self.assertNotIn(self.repo, app._preview_cache)
            app._preview_loaded(self.repo, fresh_data, app._preview_generation)
            await pilot.pause()
            self.assertEqual(app._preview_cache[self.repo], fresh_data)

    async def test_inline_agent_badges_and_open_scope_and_mouse_and_theme_picker(self):
        closed_dir = str(self.root / "closed")
        os.makedirs(closed_dir)
        app = PickerApp(
            paths_git=[self.repo, closed_dir],
            paths_all=[self.repo, closed_dir],
            current=self.repo,
            opened={self.repo},
            names={},
            herdr=FakeHerdr(),
            mapping_reader=lambda *_: {},
            src_pane="A",
            agents={self.repo: ("copilot", "codex")},
        )
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            rendered = str(app.row_list._build_text())
            badge = format_agent_badges(("copilot", "codex"))
            self.assertIn(badge, rendered)
            first_line = rendered.splitlines()[0]
            self.assertTrue(first_line.endswith(badge), repr(first_line))

            # Toggle open-workspaces-only scope with alt+o and back
            await pilot.press("alt+o")
            self.assertEqual(app.scope, "open")
            self.assertEqual([row.path for row in visible(app)], [self.repo])
            await pilot.press("alt+o")
            self.assertEqual(app.scope, "git")
            self.assertEqual([row.path for row in visible(app)], [self.repo, closed_dir])

            # Row 1 is the live/closed divider; a click there selects nothing.
            await pilot.click("#list", offset=(4, 1))
            self.assertEqual(app.index, 0)

            # Theme picker modal previews live and reverts on Escape
            original_theme = app.theme
            await pilot.press("ctrl+t")
            await pilot.pause()
            self.assertIsInstance(app.screen, ThemePickerModal)
            await pilot.press("j")
            await pilot.pause()
            self.assertNotEqual(app.theme, original_theme)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.theme, original_theme)

            # A single click on a row selects and jumps straight to it.
            await pilot.click("#list", offset=(4, 2))
            await pilot.pause()
        self.assertIsNotNone(app.return_value)
        self.assertEqual(app.return_value.path, closed_dir)


class ChoiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = str(self.root / "repo")
        os.makedirs(self.repo)

    def app(self, herdr, mapping):
        return PickerApp(
            paths_git=[self.repo],
            paths_all=[self.repo],
            current=None,
            opened=set(),
            names={},
            herdr=herdr,
            mapping_reader=lambda *_: mapping,
            src_pane="A",
        )

    async def test_unknown_workspace_asks_for_choice(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w-old", "label": "repo", "active_tab_id": "t"}],
            panes=[],
        )
        app = self.app(herdr, {})
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, SelectWorkspaceModal)
            await pilot.press("down", "enter")
            await pilot.pause()
        self.assertEqual(app.return_value.choice, ("use", "w-old"))

    async def test_cancel_choice_keeps_picker_open(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w-old", "label": "repo", "active_tab_id": "t"}],
            panes=[],
        )
        app = self.app(herdr, {})
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, SelectWorkspaceModal)
            self.assertTrue(app.is_running)
            await pilot.press("escape")
            await pilot.press("q")

    async def test_mapped_workspace_skips_modal(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w1", "label": "repo", "active_tab_id": "t"}],
            panes=[],
        )
        app = self.app(herdr, {"w1": self.repo})
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsNone(app.return_value.choice)


class AppTypeTest(unittest.TestCase):
    def test_picker_is_an_app(self):
        self.assertTrue(issubclass(PickerApp, App))


class ExtendedFilterAndModesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.alpha = str(self.root / "alpha")
        self.beta = str(self.root / "Beta")
        self.gamma = str(self.root / "gamma")
        for path in (self.alpha, self.beta, self.gamma):
            os.makedirs(path)

    def app(self):
        return PickerApp(
            paths_git=[self.alpha, self.beta, self.gamma],
            paths_all=[self.alpha, self.beta, self.gamma],
            current=None,
            opened=set(),
            names={},
            herdr=FakeHerdr(),
            mapping_reader=lambda *_: {},
            src_pane="A",
        )

    async def test_extended_search_and_smartcase_keep_rank_order(self):
        app = self.app()
        async with app.run_test():
            app.insert_text("a | Beta")
            self.assertEqual([row.path for row in visible(app)], [self.alpha, self.beta, self.gamma])
            app.clear_query()
            app.insert_text("^/tmp")
            self.assertEqual(len(visible(app)), 3)
            app.clear_query()
            app.insert_text("gamma$")
            self.assertEqual([row.path for row in visible(app)], [self.gamma])
            app.clear_query()
            app.insert_text("!Beta")
            self.assertEqual([row.path for row in visible(app)], [self.alpha, self.gamma])
            app.clear_query()
            app.insert_text("Beta")
            self.assertEqual([row.path for row in visible(app)], [self.beta])

    async def test_insert_mode_readline_and_tab_completion(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+n")
            self.assertEqual(app.index, 1)
            await pilot.press("ctrl+j")
            self.assertGreaterEqual(app.index, 0)
            await pilot.press("g", "a", "m")
            self.assertEqual(app.query, "gam")
            await pilot.press("ctrl+a")
            await pilot.press("ctrl+u")
            self.assertEqual(app.query, "gam")
            await pilot.press("ctrl+e")
            await pilot.press("ctrl+u")
            self.assertEqual(app.query, "")
            await pilot.press("ctrl+y")
            self.assertEqual(app.query, "gam")
            app.clear_query()
            await pilot.press("a", "l", "p", "h", "a", "/", "x")
            await pilot.press("ctrl+w")
            selected = app.filtered[app.index].path
            await pilot.press("tab")
            self.assertEqual(app.query, selected)

    async def test_word_deletion_skips_trailing_separators(self):
        app = self.app()
        async with app.run_test() as pilot:
            app.insert_text("alpha   ")
            await pilot.press("ctrl+w")
            self.assertEqual(app.query, "")
            app.insert_text("alpha   ")
            await pilot.press("alt+backspace")
            self.assertEqual(app.query, "")
            app.insert_text("beta/")
            await pilot.press("ctrl+w")
            self.assertEqual(app.query, "")
            app.insert_text("beta/")
            await pilot.press("alt+backspace")
            self.assertEqual(app.query, "")

    async def test_word_deletion_still_stops_at_the_previous_segment(self):
        app = self.app()
        async with app.run_test() as pilot:
            app.insert_text("alpha/beta")
            await pilot.press("ctrl+w")
            self.assertEqual(app.query, "alpha")

    async def test_anchored_query_matches_path_and_label(self):
        names = {self.beta: "beta-label"}
        app = PickerApp(
            paths_git=[self.alpha, self.beta, self.gamma],
            paths_all=[self.alpha, self.beta, self.gamma],
            current=None,
            opened=set(),
            names=names,
            herdr=FakeHerdr(),
            mapping_reader=lambda *_: {},
            src_pane="A",
        )
        async with app.run_test():
            app.insert_text("^" + self.alpha + "$")
            self.assertEqual([row.path for row in visible(app)], [self.alpha])
            app.clear_query()
            app.insert_text("^" + self.alpha + "x$")
            self.assertEqual(app.filtered, [])
            app.clear_query()
            shown = next(row.shown for row in app.rows if row.path == self.beta)
            self.assertIn("[beta-label]", shown)
            app.insert_text("^" + shown + "$")
            self.assertEqual([row.path for row in visible(app)], [self.beta])
            app.clear_query()
            app.insert_text("[beta-label]$")
            self.assertEqual([row.path for row in visible(app)], [self.beta])

    async def test_normal_mode_navigation_and_help(self):
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("escape")
            self.assertEqual(app.mode, "normal")
            await pilot.press("j", "k", "G", "g", "g", "H", "M", "L")
            await pilot.press("d", "d")
            self.assertEqual(app.query, "")
            await pilot.press("C")
            self.assertEqual(app.mode, "insert")
            await pilot.press("/")
            self.assertEqual(app.mode, "insert")
            await pilot.press("escape")
            await pilot.press("i")
            self.assertEqual(app.mode, "insert")
            await pilot.press("escape")
            await pilot.press("?")
            await pilot.pause()
            self.assertIsNotNone(app.screen)
            await pilot.press("escape")

    async def test_alt_a_and_move_shortcuts(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("alt+a")
            self.assertTrue(app.show_all)
            await pilot.press("alt+p")
            await pilot.pause()
        self.assertEqual(app.return_value.kind, "move-pane")

        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("escape", "t")
            await pilot.pause()
        self.assertEqual(app.return_value.kind, "move-tab")

    async def test_typed_path_fallback_and_preview_hide(self):
        typed = str(self.root / "typed")
        os.makedirs(typed)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.insert_text(typed)
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(app.return_value.path, typed)

        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            preview = app.query_one("#preview")
            self.assertFalse(preview.display)


class CloseRevalidationTupleTest(unittest.IsolatedAsyncioTestCase):
    async def test_reprompts_when_new_agent_tuple_appears(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = str(root / "repo")
        os.makedirs(repo)
        workspaces = [
            {"workspace_id": "A", "label": "repo", "active_tab_id": "t"},
            {"workspace_id": "B", "label": "repo", "active_tab_id": "t"},
        ]
        panes_first = [
            {"workspace_id": "A", "pane_id": "p1", "tab_id": "t", "cwd": repo, "agent": "copilot"},
            {"workspace_id": "B", "pane_id": "p2", "tab_id": "t", "cwd": repo},
        ]
        panes_second = [
            {"workspace_id": "A", "pane_id": "p1", "tab_id": "t", "cwd": repo, "agent": "copilot"},
            {"workspace_id": "B", "pane_id": "p2", "tab_id": "t", "cwd": repo, "agent": "copilot"},
        ]
        herdr = FakeHerdr(workspaces=workspaces, panes=panes_first)
        app = PickerApp(
            paths_git=[repo],
            paths_all=[repo],
            current=None,
            opened={repo},
            names={},
            herdr=herdr,
            mapping_reader=lambda *_: {"A": repo, "B": repo},
            src_pane="A",
        )
        async with app.run_test() as pilot:
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmCloseModal)
            herdr._panes = panes_second
            await pilot.press("y")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmCloseModal)
            self.assertEqual(herdr.closed, [])


class SequencedPaneHerdr(FakeHerdr):
    """Serves one pane snapshot per ``panes()`` call outside preview workers."""

    def __init__(self, workspaces, snapshots):
        super().__init__(workspaces=workspaces, panes=snapshots[0])
        self.snapshots = list(snapshots)
        self.served = 0

    def panes(self):
        index = min(self.served, len(self.snapshots) - 1)
        try:
            group = get_current_worker().group
        except NoActiveWorker:
            group = ""
        if group not in ("preview", "prefetch"):
            self.served += 1
        return [dict(pane) for pane in self.snapshots[index]]


class CloseRevalidationLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = str(self.root / "repo")
        os.makedirs(self.repo)
        self.workspaces = [
            {"workspace_id": "A", "label": "repo", "active_tab_id": "t"},
            {"workspace_id": "B", "label": "repo", "active_tab_id": "t"},
        ]

    def pane(self, pane_id, agent=None, workspace_id="A"):
        pane = {"workspace_id": workspace_id, "pane_id": pane_id, "tab_id": "t", "cwd": self.repo}
        if agent is not None:
            pane["agent"] = agent
        return pane

    def app(self, herdr, mapping=None):
        return PickerApp(
            paths_git=[self.repo],
            paths_all=[self.repo],
            current=None,
            opened={self.repo},
            names={},
            herdr=herdr,
            mapping_reader=lambda *_: mapping if mapping is not None else {},
            src_pane="A",
        )

    async def wait_for_modal(self, app, pilot):
        for _ in range(200):
            if isinstance(app.screen, ConfirmCloseModal):
                return True
            await pilot.pause(0.01)
        return False

    async def test_mutating_state_during_the_second_modal_asks_a_third_time(self):
        snapshots = [
            [self.pane("p1", "copilot")],
            [self.pane("p1", "copilot"), self.pane("p2", "zed", workspace_id="B")],
            [
                self.pane("p1", "copilot"),
                self.pane("p2", "zed", workspace_id="B"),
                self.pane("p3", "gpt", workspace_id="B"),
            ],
        ]
        herdr = SequencedPaneHerdr(self.workspaces, snapshots)
        app = self.app(herdr, mapping={"A": self.repo, "B": self.repo})
        async with app.run_test() as pilot:
            await pilot.press("ctrl+x")
            self.assertTrue(await self.wait_for_modal(app, pilot))
            await pilot.press("y")
            self.assertTrue(await self.wait_for_modal(app, pilot))
            self.assertEqual(herdr.closed, [])
            await pilot.press("y")
            self.assertTrue(await self.wait_for_modal(app, pilot))
            self.assertEqual(herdr.closed, [])
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(herdr.closed, [])
            self.assertTrue(app.is_running)

    async def test_only_targets_from_the_latest_snapshot_are_closed(self):
        snapshots = [
            [self.pane("p1"), self.pane("p2", workspace_id="B")],
            [self.pane("p1")],
        ]
        herdr = SequencedPaneHerdr(self.workspaces, snapshots)
        app = self.app(herdr)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertEqual(herdr.closed, ["A"])

    async def test_close_gives_up_after_the_round_cap(self):
        repo = self.repo
        workspaces = self.workspaces

        class GrowingHerdr(FakeHerdr):
            def __init__(self):
                super().__init__(workspaces=workspaces, panes=[])
                self.served = 0

            def panes(self):
                self.served += 1
                return [
                    {
                        "workspace_id": "A",
                        "pane_id": f"p{self.served}",
                        "tab_id": "t",
                        "cwd": repo,
                        "agent": f"agent{self.served}",
                    }
                ]

        herdr = GrowingHerdr()
        app = self.app(herdr, mapping={"A": self.repo})
        async with app.run_test() as pilot:
            await pilot.press("ctrl+x")
            await pilot.pause()
            for _ in range(MAX_CLOSE_ROUNDS + 2):
                if isinstance(app.screen, ConfirmCloseModal):
                    await pilot.press("y")
                    await pilot.pause()
            self.assertEqual(herdr.closed, [])
            self.assertIn("Close aborted", app.error)

    async def test_close_aborts_on_an_untrusted_mapping_file(self):
        herdr = FakeHerdr(workspaces=self.workspaces, panes=[self.pane("p1")])
        broken = StateError(Path(self.root / "state.json"))

        def reader(*_):
            raise broken

        app = PickerApp(
            paths_git=[self.repo],
            paths_all=[self.repo],
            current=None,
            opened={self.repo},
            names={},
            herdr=herdr,
            mapping_reader=reader,
            strict_mapping_reader=reader,
            src_pane="A",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmCloseModal)
            self.assertEqual(herdr.closed, [])
            self.assertIn("Invalid workspace mapping", app.error)
            self.assertIn("state.json", app.error)


class RankingRefreshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git_a = str(self.root / "git-a")
        self.git_b = str(self.root / "git-b")
        self.plain = str(self.root / "plain")
        for path in (self.git_a, self.git_b, self.plain):
            os.makedirs(path)
        for path in (self.git_a, self.git_b):
            os.makedirs(os.path.join(path, ".git"))
        self.history = [self.plain, self.git_a, self.git_b]
        self.mapping = {"w-plain": self.plain}

    async def test_closing_an_open_non_git_directory_reranks(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w-plain", "label": "plain", "active_tab_id": "t"}],
            panes=[{"workspace_id": "w-plain", "pane_id": "p1", "tab_id": "t", "cwd": self.plain}],
        )
        opened = {self.plain}
        app = PickerApp(
            paths_git=rank_paths(self.history, None, opened, False),
            paths_all=rank_paths(self.history, None, opened, True),
            current=None,
            opened=opened,
            names={},
            herdr=herdr,
            mapping_reader=lambda *_: self.mapping,
            history=self.history,
            src_pane="A",
        )
        async with app.run_test() as pilot:
            self.assertEqual(
                [row.path for row in app.rows], [self.plain, self.git_a, self.git_b]
            )
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertEqual(herdr.closed, ["w-plain"])
            self.assertEqual(app.opened, set())
            self.assertNotIn(self.plain, [row.path for row in app.rows])
            expected = rank_paths(self.history, None, set(), False)
            self.assertEqual([row.path for row in app.rows], expected)
            self.assertEqual(app.paths["git"], expected)
            self.assertEqual(
                app.paths["all"], rank_paths(self.history, None, set(), True)
            )


class PresentationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = str(self.root / "repo")
        self.other = str(self.root / "other")
        os.makedirs(self.repo)
        os.makedirs(self.other)

    def app(self, **kwargs):
        options = {
            "paths_git": [self.repo, self.other],
            "paths_all": [self.repo, self.other],
            "current": self.repo,
            "opened": {self.other},
            "names": {},
            "herdr": FakeHerdr(),
            "mapping_reader": lambda *_: {},
            "src_pane": "A",
        }
        options.update(kwargs)
        return PickerApp(**options)

    @staticmethod
    def text(app, selector):
        return str(app.query_one(selector).render())

    async def test_footer_switches_with_mode_and_condenses_when_narrow(self):
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            insert = self.text(app, "#status")
            self.assertIn("all dirs", insert)
            self.assertIn("Esc normal", insert)
            self.assertNotIn("^H", insert)
            self.assertNotIn("ctrl-h", insert)
            await pilot.press("escape")
            normal = self.text(app, "#status")
            self.assertIn("j/k", normal)
            self.assertIn("q quit", normal)
            self.assertNotEqual(insert, normal)

        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            footer = self.text(app, "#status")
            self.assertLessEqual(len(footer), app.query_one("#status").content_size.width)
            self.assertIn("M-p/t", footer)
            self.assertIn("? help", footer)

    async def test_title_shows_move_badge_and_dataset_hint(self):
        app = self.app(move_mode="pane")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            bar = self.text(app, "#query")
            self.assertTrue(bar.startswith(" move pane "), bar)
            self.assertTrue(bar.endswith("[git]"), bar)
            badge = [
                span
                for span in app.query_one("#query").render().spans
                if span.start == 0 and (" on " in str(span.style) or "reverse" in str(span.style))
            ]
            self.assertTrue(badge, "the move chip must carry a filled background")
            await pilot.press("alt+a")
            self.assertTrue(self.text(app, "#query").endswith("[all]"))

    async def test_prompt_bar_pins_the_match_count_to_the_right_edge(self):
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            bar = self.text(app, "#query")
            self.assertTrue(bar.endswith("2/2  [git]"), repr(bar))
            self.assertEqual(len(bar), app.query_one("#query").content_size.width)
            app.insert_text("repo")
            await pilot.pause()
            bar = self.text(app, "#query")
            self.assertTrue(bar.endswith("1/2  [git]"), repr(bar))

        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.resize_terminal(90, 30)
            await pilot.pause()
            bar = self.text(app, "#query")
            self.assertTrue(bar.endswith("2/2  [git]"), repr(bar))
            self.assertEqual(len(bar), app.query_one("#query").content_size.width)

    async def test_preview_renders_sections_with_a_key_column(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.preview_data = PreviewData(
                path=self.repo,
                label="repo",
                workspaces=(
                    WorkspaceInfo(
                        "w1",
                        tabs=("t1", "t2"),
                        agents=("copilot",),
                        sessions=(
                            SessionInfo("copilot", "idle", "Fix auth bug"),
                            SessionInfo(
                                "pi",
                                "working",
                                "Review PR #42 and verify long session titles wrap cleanly across multiple lines",
                            ),
                        ),
                    ),
                ),
                git=GitInfo(
                    branch="main",
                    staged=1,
                    modified=2,
                    untracked=1,
                    ahead=1,
                    is_worktree=True,
                    commits=(
                        CommitInfo("a1b2c3d", "2h ago", "feat: add vim normal mode " + "x" * 200),
                        CommitInfo("d4e5f6a", "1d ago", "fix: layout"),
                    ),
                ),
            )
            app.update_preview()
            preview = self.text(app, "#preview")
            for needle in (
                "repo",
                self.repo,
                "Current workspace (w1)",
                "tabs  t1, t2",
                f"{agent_icon('copilot')} copilot  \u25cb idle",
                "\u2502 Fix auth bug",
                f"{agent_icon('pi')} pi  \u25cf working",
                "\u2502 Review PR #42",
                "wrap cleanly across",
                "\u2387 main  worktree  +1 ~2 ?1  \u21911",
                "a1b2c3d 2h ago",
                "d4e5f6a 1d ago",
                "\u2026",
            ):
                self.assertIn(needle, preview)

            app.preview_data = PreviewData(path=self.repo, label="repo", loading=True)
            app.update_preview()
            preview = self.text(app, "#preview")
            self.assertIn("Checking workspace\u2026", preview)
            self.assertNotIn("\u2387", preview)

    async def test_selected_row_has_no_foreground_but_fills_the_width(self):
        app = self.app()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            text = app.row_list._build_text()
            lines = text.split("\n")
            start = len(lines[0]) + 1
            width = len(lines[1])
            bar = [
                span
                for span in text.spans
                if span.start == start
                and span.end == start + width
                and str(span.style).startswith("on #")
            ]
            self.assertTrue(bar, [(s.start, s.end, str(s.style)) for s in text.spans])
            inside = [
                span
                for span in text.spans
                if span.start >= start and span.end <= start + width
                and not str(span.style).startswith("on #")
            ]
            # Tinted dark selection preserves rich per-token foreground styling.
            self.assertTrue(inside)

            # When falling back to non-hex reverse video, foreground colours are omitted.
            with mock.patch.object(app, "selection_style", return_value="bold reverse"):
                app.row_list.selected_row_style = app.selection_style()
                fallback_text = app.row_list._build_text()
                fallback_inside = [
                    span
                    for span in fallback_text.spans
                    if span.start >= start
                    and span.end <= start + width
                    and str(span.style) != "bold reverse"
                ]
                for span in fallback_inside:
                    style = str(span.style)
                    self.assertNotIn("#", style)
                    for colour in ("yellow", "cyan", "dim", "green", "red", "blue"):
                        self.assertNotIn(colour, style)

    async def test_long_query_is_windowed_around_the_cursor(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query = "segment/" * 40
            app.cursor = len(app.query)
            app.update_status()
            bar = self.text(app, "#query")
            self.assertIn("\u2026", bar)
            self.assertLessEqual(len(bar), app.query_one("#query").content_size.width)
            self.assertTrue(bar.rstrip().endswith("2/2  [git]"))
            cursor = [
                span
                for span in app.query_one("#query").render().spans
                if getattr(span.style, "reverse", False)
            ]
            self.assertTrue(cursor, "the block cursor must stay visible")

    async def test_right_aligned_fields_follow_growing_and_shrinking_terminal(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for width in (120, 80):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                widget = app.query_one("#query")
                rendered = widget.render()
                self.assertEqual(cell_len(rendered.plain), widget.content_size.width)
                self.assertTrue(self.text(app, "#query").endswith("2/2  [git]"))

    async def test_bars_track_every_single_column_resize(self):
        app = self.app()
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            for width in (121, 122, 123, 124, 125, 124, 123, 122):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                widget = app.query_one("#query")
                rendered = widget.render()
                self.assertEqual(
                    cell_len(rendered.plain),
                    widget.content_size.width,
                    f"#query at {width} columns",
                )
                footer = app.query_one("#status")
                self.assertLessEqual(cell_len(str(footer.render())), footer.content_size.width)
                self.assertTrue(self.text(app, "#query").endswith("2/2  [git]"))

    async def test_layout_rules_live_in_default_css(self):
        css = PickerApp.DEFAULT_CSS
        self.assertIn("overflow-x: hidden", css)
        self.assertIn("#preview", css)
        self.assertIn("width: 40%", css)

        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            rows = app.query_one("#list")
            self.assertEqual(rows.styles.overflow_x, "hidden")
            self.assertEqual(app.query_one("#preview").styles.width.value, 40.0)

    async def test_wide_query_and_error_fit_the_prompt_bar_in_cells(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            query = "\u754c" * 100
            prefix_width = len("git \u203a ")
            for cursor in (0, 50, len(query)):
                app.query = query
                app.cursor = cursor
                app.update_status()
                await pilot.pause()
                widget = app.query_one("#query")
                rendered = widget.render()
                bar = rendered.plain
                self.assertLessEqual(cell_len(bar), widget.content_size.width)
                self.assertNotIn("\n", bar)
                expected = query[cursor] if cursor < len(query) else " "
                cursor_spans = [
                    span
                    for span in rendered.spans
                    if getattr(span.style, "reverse", False)
                    and span.start >= prefix_width
                ]
                self.assertEqual(len(cursor_spans), 1, bar)
                span = cursor_spans[0]
                self.assertEqual(bar[span.start], expected)
                self.assertLess(span.start, widget.content_size.width)
            app.error = "E" * 100
            app.update_status()
            await pilot.pause()
            widget = app.query_one("#query")
            bar = widget.render().plain
            self.assertEqual(cell_len(bar), widget.content_size.width)
            self.assertNotIn("\n", bar)

    async def test_preview_keeps_every_workspace_detail(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.preview_data = PreviewData(
                path=self.repo,
                label="repo",
                workspaces=(
                    WorkspaceInfo("w1", tabs=("t1",), agents=("copilot",)),
                    WorkspaceInfo("w2", tabs=("t2",), agents=("codex",)),
                ),
            )
            app.update_preview()
            preview = self.text(app, "#preview")
            for needle in (
                "tabs  t1",
                f"agents  {agent_icon('copilot')} copilot",
                "tabs  t2",
                f"agents  {agent_icon('codex')} codex",
            ):
                self.assertIn(needle, preview)

    def test_git_status_v2_and_log_parsing(self):
        status = "\n".join(
            [
                "# branch.oid abc123",
                "# branch.head feat/preview",
                "# branch.ab +2 -1",
                "1 M. N... 100644 100644 100644 abc def file_a.py",
                "1 .M N... 100644 100644 100644 abc def file_b.py",
                "1 MM N... 100644 100644 100644 abc def file_c.py",
                "? untracked.txt",
            ]
        )
        parsed = _parse_git_status_v2(status)
        self.assertEqual(
            parsed,
            {
                "branch": "feat/preview",
                "ahead": 2,
                "behind": 1,
                "staged": 2,
                "modified": 2,
                "untracked": 1,
            },
        )
        commits = _parse_git_log("a1b2c3d\t2 hours ago\tfeat: one\nd4e5f6a\t1 day ago\tfix: two\n")
        self.assertEqual(
            commits,
            (
                CommitInfo("a1b2c3d", "2h", "feat: one"),
                CommitInfo("d4e5f6a", "1d", "fix: two"),
            ),
        )

    async def test_help_fits_a_small_terminal(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_help()
            for _ in range(100):
                if isinstance(app.screen, HelpModal):
                    break
                await pilot.pause(0.01)
            self.assertIsInstance(app.screen, HelpModal)
            panel = app.screen.query_one("#help")
            region = panel.region
            self.assertGreaterEqual(region.x, 0)
            self.assertGreaterEqual(region.y, 0)
            self.assertLessEqual(region.x + region.width, 80)
            self.assertLessEqual(region.y + region.height, 24)
            rendered = str(panel.render())
            self.assertIn("quit", rendered)
            line = rendered[: rendered.index("quit")].count("\n")
            self.assertLess(line, region.height)

    async def test_design_details_use_themed_pill_and_keycaps(self):
        app = self.app(names={self.other: "project-y"}, opened=set(), theme_setting="herdr-ink")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            palette = app.palette()
            text = app.row_list._build_text()
            suffix = [
                span
                for span in text.spans
                if "italic" in str(span.style) and palette.secondary in str(span.style)
            ]
            self.assertTrue(suffix, [str(span.style) for span in text.spans])
            await pilot.press("escape")
            footer = self.text(app, "#status")
            self.assertIn("i/a/\u2571 filter", footer)
            pill = [
                span
                for span in app.query_one("#query").render().spans
                if span.start == 0
            ]
            self.assertTrue(pill)
            self.assertIn(" on ", str(pill[0].style))

    async def test_confirm_and_select_modals_keep_the_approved_labels(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(ConfirmCloseModal("repo", []))
            await pilot.pause()
            confirm_panel = app.screen.query_one("#confirm")
            self.assertEqual(confirm_panel.border_title, " Close workspace ")
            confirm = self.text(app.screen, "#confirm")
            self.assertIn("[y/N]", confirm)
            await pilot.press("n")
            await pilot.pause()
            app.push_screen(SelectWorkspaceModal("repo", ["w-old"]))
            await pilot.pause()
            select = self.text(app.screen, "#select")
            self.assertTrue(select.startswith("Select workspace for repo"), select)
            panel = app.screen.query_one("#select")
            self.assertEqual(panel.border_title, " Workspace conflict ")

    async def test_theme_sync_and_preview_toggle(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}, clear=False):
            default_app = self.app(theme_setting="auto")
            self.assertEqual(default_app.theme, "herdr-ink")

            herdr_cfg = self.root / "herdr" / "config.toml"
            herdr_cfg.parent.mkdir(parents=True, exist_ok=True)
            herdr_cfg.write_text('[theme]\nname = "tokyonight"\n')
            synced_app = self.app(theme_setting="auto")
            self.assertEqual(synced_app.theme, "tokyo-night")

        with self.assertRaises(SettingsError):
            self.app(theme_setting="no-such-theme")

        app = self.app(theme_setting="herdr-ink")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#preview").display)
            await pilot.press("alt+v")
            await pilot.pause()
            self.assertFalse(app.query_one("#preview").display)
            await pilot.press("escape")
            await pilot.press("v")
            await pilot.pause()
            self.assertTrue(app.query_one("#preview").display)


class TabTreeAndRankingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.open_ws = self.root / "api" / "frontend"
        self.open_ws.mkdir(parents=True)
        self.closed_exact = self.root / "api"
        self.closed_sub = self.root / "my-api"
        self.closed_sub.mkdir(parents=True)
        self.tabs = {
            str(self.open_ws): (
                TabEntry(
                    workspace_id="w1",
                    tab_id="w1:t1",
                    label="Analyze Local Changes",
                    number=1,
                    active=True,
                    focused=True,
                    agents=("pi",),
                    agent_status="working",
                ),
                TabEntry(
                    workspace_id="w1",
                    tab_id="w1:t2",
                    label="zsh",
                    number=2,
                ),
            )
        }

    def make_app(self, **overrides):
        params = dict(
            paths_git=[str(self.open_ws), str(self.closed_sub), str(self.closed_exact)],
            paths_all=[str(self.open_ws), str(self.closed_sub), str(self.closed_exact)],
            current=None,
            opened={str(self.open_ws)},
            names={},
            herdr=FakeHerdr(),
            mapping_reader=lambda *_: {"w1": str(self.open_ws)},
            src_pane="w1:p1",
            agents={str(self.open_ws): ("pi",)},
            tabs=self.tabs,
        )
        params.update(overrides)
        return PickerApp(**params)

    async def test_unicode_tab_tree_renders_agent_and_jumps_to_tab(self):
        app = self.make_app()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(len(visible(app)), 5)
            self.assertFalse(app.filtered[0].is_tab)
            self.assertTrue(app.filtered[1].is_tab)
            self.assertEqual(app.filtered[1].tree_prefix, "├─ ")
            self.assertEqual(app.filtered[2].tree_prefix, "└─ ")

            ws_line = app.row_list._row_text(0, 90).plain
            self.assertNotIn("\u03c0", ws_line)
            self.assertEqual(app.filtered[0].agents, ())

            tab_line = app.row_list._row_text(1, 90).plain
            self.assertIn("├─ ", tab_line)
            self.assertIn("Analyze Local Changes", tab_line)
            self.assertIn("\u03c0 pi \u00b7 working", tab_line)

            shell_line = app.row_list._row_text(2, 90).plain
            self.assertIn("└─ ", shell_line)
            self.assertIn("zsh", shell_line)
            self.assertNotIn("\u03c0", shell_line)

            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsNotNone(app.return_value)
        assert app.return_value is not None
        self.assertEqual(app.return_value.kind, "jump")
        self.assertEqual(app.return_value.path, str(self.open_ws))
        self.assertEqual(app.return_value.target_workspace_id, "w1")
        self.assertEqual(app.return_value.target_tab_id, "w1:t1")

    async def test_toggle_tabs_and_filter_by_agent_or_tab_label(self):
        app = self.make_app()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await pilot.press("alt+e")
            await pilot.pause()
            self.assertFalse(any(row.is_tab for row in app.filtered))
            await pilot.press("alt+e")
            await pilot.pause()
            self.assertTrue(any(row.is_tab for row in app.filtered))

            # Searching by agent keyword keeps only the parent workspace + matching tab with └
            app.insert_text("agent")
            await pilot.pause()
            self.assertEqual(len(visible(app)), 2)
            self.assertFalse(app.filtered[0].is_tab)
            self.assertEqual(app.filtered[0].path, str(self.open_ws))
            self.assertTrue(app.filtered[1].is_tab)
            self.assertEqual(app.filtered[1].tab_id, "w1:t1")
            self.assertEqual(app.filtered[1].tree_prefix, "└─ ")

    async def test_collapse_and_expand_with_left_right_toggles_workspace_agents(self):
        app = self.make_app()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            # Expanded by default: no agent badge on workspace row, tabs visible.
            self.assertEqual(len(visible(app)), 5)
            self.assertEqual(app.filtered[0].agents, ())
            self.assertNotIn("\u03c0", app.row_list._row_text(0, 90).plain)

            # Move onto a child tab and press left -> collapses parent, selects parent, shows agent badge.
            await pilot.press("down")
            self.assertTrue(app.filtered[app.index].is_tab)
            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(len(visible(app)), 3)
            self.assertEqual(app.index, 0)
            self.assertFalse(app.filtered[0].is_tab)
            self.assertEqual(app.filtered[0].agents, ("pi",))
            collapsed_line = app.row_list._row_text(0, 90).plain
            self.assertIn("\u25b8 2", collapsed_line)
            self.assertIn("\u03c0", collapsed_line)

            # Press right on collapsed workspace -> expands tabs and hides workspace agent badge.
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(len(visible(app)), 5)
            self.assertEqual(app.filtered[0].agents, ())
            self.assertNotIn("\u03c0", app.row_list._row_text(0, 90).plain)
            self.assertTrue(app.filtered[1].is_tab)

    async def test_query_working_pill_tab_preview_and_workspace_jump(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            query_text = app._query_text(100).plain
            self.assertIn("\u25cf 1 working", query_text)

            # Hovering a tab row highlights it in the preview pane.
            await pilot.press("down")
            await pilot.pause()
            app.preview_data = PreviewData(
                path=str(self.open_ws),
                label="frontend",
                workspaces=(WorkspaceInfo(workspace_id="w1", tabs=("w1:t1", "w1:t2")),),
            )
            app.update_preview()
            preview_text = app._preview_text().plain
            self.assertIn("\u25b8 Analyze Local Changes", preview_text)
            self.assertIn("Jump directly to this tab", preview_text)
            self.assertIn("zsh", preview_text)

            # ] / [ in normal mode skips over tab rows directly to next/prev workspace.
            await pilot.press("escape")
            await pilot.press("]")
            await pilot.pause()
            self.assertFalse(app.filtered[app.index].is_tab)
            self.assertEqual(app.filtered[app.index].path, str(self.closed_sub))
            await pilot.press("[")
            await pilot.pause()
            self.assertEqual(app.index, 0)
            self.assertEqual(app.filtered[app.index].path, str(self.open_ws))

            # Space toggles fold in normal mode.
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(len(visible(app)), 3)
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(len(visible(app)), 5)

    async def test_yank_multiple_tabs_esc_clears_and_p_moves_tabs(self):
        class MoveHerdr(FakeHerdr):
            def __init__(self, workspaces, panes, tabs_list):
                super().__init__(workspaces=workspaces, panes=panes)
                self._tabs = tabs_list
                self.moves: list[tuple[str, ...]] = []

            def tabs(self):
                return list(self._tabs)

            def pane_layout(self, pane_id):
                pane = next(p for p in self._panes if p["pane_id"] == pane_id)
                return {
                    "area": {"x": 0, "y": 0, "width": 100, "height": 40},
                    "panes": [
                        {
                            "pane_id": pane_id,
                            "rect": {"x": 0, "y": 0, "width": 100, "height": 40},
                        }
                    ],
                    "splits": [],
                    "tab_id": pane["tab_id"],
                    "workspace_id": pane["workspace_id"],
                }

            def pane_move(self, *args):
                self.moves.append(args)
                ws_id = args[args.index("--workspace") + 1] if "--workspace" in args else "w-new"
                return {"workspace_id": ws_id, "tab_id": f"{ws_id}:t-new", "pane_id": args[0]}

        workspaces = [
            {"workspace_id": "w1", "label": "frontend", "active_tab_id": "w1:t1"},
            {"workspace_id": "w2", "label": "my-api", "active_tab_id": "w2:t1"},
        ]
        panes = [
            {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1", "cwd": str(self.open_ws), "agent": "pi", "agent_status": "working"},
            {"workspace_id": "w1", "tab_id": "w1:t2", "pane_id": "w1:p2", "cwd": str(self.open_ws), "agent": None},
            {"workspace_id": "w2", "tab_id": "w2:t1", "pane_id": "w2:p1", "cwd": str(self.closed_sub), "agent": None},
        ]
        raw_tabs = [
            {"workspace_id": "w1", "tab_id": "w1:t1", "label": "Analyze Local Changes", "number": 1, "focused": True, "agent_status": "working"},
            {"workspace_id": "w1", "tab_id": "w1:t2", "label": "zsh", "number": 2, "focused": False, "agent_status": None},
            {"workspace_id": "w2", "tab_id": "w2:t1", "label": "server", "number": 1, "focused": False, "agent_status": None},
        ]
        herdr = MoveHerdr(workspaces, panes, raw_tabs)
        all_tabs = {
            str(self.open_ws): self.tabs[str(self.open_ws)],
            str(self.closed_sub): (
                TabEntry(workspace_id="w2", tab_id="w2:t1", label="server", number=1, active=True),
            ),
        }
        app = self.make_app(
            herdr=herdr,
            opened={str(self.open_ws), str(self.closed_sub)},
            mapping_reader=lambda *_: {"w1": str(self.open_ws), "w2": str(self.closed_sub)},
            tabs=all_tabs,
            start_mode="normal",
        )
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Yank both tabs in w1.
            await pilot.press("j")  # w1:t1
            await pilot.press("y")
            await pilot.press("j")  # w1:t2
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(set(app.yanked_tabs), {"w1:t1", "w1:t2"})
            self.assertTrue(app.filtered[1].yanked)
            self.assertTrue(app.filtered[2].yanked)
            self.assertIn("\u2713", app.row_list._row_text(1, 90).plain)
            self.assertIn("2 yanked", app._query_text(100).plain)

            # Esc clears yanks without exiting.
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.yanked_tabs, {})
            self.assertFalse(app.filtered[1].yanked)

            # Yank both tabs again, move to w2:t1 tab row, and press p to paste there.
            await pilot.press("y")  # w1:t2
            await pilot.press("k")  # w1:t1
            await pilot.press("y")  # w1:t1
            await pilot.press("j", "j", "j")  # w2 -> w2:t1
            self.assertEqual(app.filtered[app.index].tab_id, "w2:t1")
            await pilot.press("p")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(app.yanked_tabs, {})
            self.assertIsNone(app.return_value)
            self.assertEqual(app.focus_after_exit, ("w2", "w2:t-new"))
            self.assertEqual(
                herdr.moves,
                [
                    ("w1:p2", "--new-tab", "--workspace", "w2", "--label", "zsh"),
                    ("w1:p1", "--new-tab", "--workspace", "w2", "--label", "Analyze Local Changes"),
                ],
            )

    async def test_group_divider_separates_live_rows_and_is_skipped(self):
        app = self.make_app()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            dividers = [i for i, row in enumerate(app.filtered) if row.is_divider]
            self.assertEqual(len(dividers), 1)
            divider = dividers[0]
            self.assertTrue(app.filtered[divider - 1].path in app.opened)
            self.assertFalse(app.filtered[divider + 1].path in app.opened)
            self.assertIn("closed", app.row_list._divider_text(60).plain)

            app.set_index(divider - 1)
            await pilot.press("down")
            self.assertEqual(app.index, divider + 1)
            await pilot.press("up")
            self.assertEqual(app.index, divider - 1)

            # The divider never counts towards the match total.
            self.assertEqual(len(visible(app)), len(app.filtered) - 1)

    async def test_open_rated_higher_most_of_the_time_on_filter(self):
        # Closed exact basename 'api' beats weak open path match '~/.../api/frontend',
        # while open '~/.../api/frontend' still beats closed non-exact 'my-api'.
        app = self.make_app(show_tabs=False)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            app.insert_text("api")
            await pilot.pause()
            self.assertEqual(
                [row.path for row in visible(app)],
                [str(self.closed_exact), str(self.open_ws), str(self.closed_sub)],
            )


if __name__ == "__main__":
    unittest.main()
