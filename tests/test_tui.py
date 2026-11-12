import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from textual.app import App
from textual.containers import VerticalScroll

from herdr_zshz.frecency import rank_paths
from herdr_zshz.state import StateError
from herdr_zshz.tui import (
    MAX_CLOSE_ROUNDS,
    ConfirmCloseModal,
    PickerApp,
    SelectWorkspaceModal,
)


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
            "mapping_reader": lambda: self.mapping,
            "src_pane": "A",
        }
        options.update(kwargs)
        return PickerApp(**options)

    async def test_typing_filters_rows(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("r", "e", "p", "o")
            self.assertEqual(app.query, "repo")
            self.assertEqual([row.path for row in app.filtered], [self.repo])
            await pilot.press("escape")
            self.assertEqual(app.mode, "normal")
            await pilot.press("g", "g")
            self.assertEqual(app.index, 0)
            await pilot.press("G")
            self.assertEqual(app.index, 0)
            await pilot.press("d", "d")
            self.assertEqual(app.query, "")
            self.assertEqual(len(app.filtered), 2)

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
            self.assertEqual([row.path for row in app.filtered], [self.repo])

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
            container = app.query_one("#list-scroll", VerticalScroll)
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
            container = app.query_one("#list-scroll", VerticalScroll)
            container.scroll_to(y=7, animate=False)
            await pilot.pause()
            self.assertEqual(int(container.scroll_offset.y), 7)
            await pilot.press("escape")
            await pilot.press("H")
            self.assertEqual(app.index, 7)
            self.assertNotEqual(app.index, 0)

    async def test_cursor_moves_without_scrolling_the_list(self):
        paths = [str(self.root / f"c{index:03}") for index in range(40)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            container = app.query_one("#list-scroll", VerticalScroll)
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
            self.assertFalse(app.query_one("#list-scroll", VerticalScroll).can_focus)
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
            # Reverse video alone can be invisible, so the row carries an
            # explicit background taken from the theme's primary colour.
            style = app.row_list.selected_row_style
            self.assertNotEqual(style, "bold reverse")
            self.assertIn(" on ", style)
            highlighted = [
                span
                for span in text.spans
                if span.start == len(lines[0]) + 1 and " on " in str(span.style)
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
            text = app.row_list.render()
            lines = str(text).split("\n")
            # One screen line per row: wrapping would shift every later row and
            # hide the cursor.
            self.assertEqual(len([line for line in lines if line]), len(paths))
            for line in lines:
                self.assertLessEqual(len(line), 70)
            self.assertIn("\u2026", lines[1])

    async def test_selection_bar_spans_the_whole_column(self):
        paths = [str(self.root / ("wide-row-" * 20 + f"{index}")) for index in range(4)]
        for path in paths:
            os.makedirs(path, exist_ok=True)
        app = self.app(paths_git=paths, paths_all=paths)
        async with app.run_test(size=(70, 16)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            text = app.row_list.render()
            lines = str(text).split("\n")
            self.assertEqual(len(lines[0]), 70)
            self.assertEqual(len(lines[1]), 70)
            start = len(lines[0]) + 1
            padded = [
                span
                for span in text.spans
                if span.start <= start
                and span.end >= start + 70
                and " on " in str(span.style)
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
            self.assertIn(self.repo, "\n".join(app.preview_lines))


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
            mapping_reader=lambda: self.mapping,
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

    async def wait_for_preview(self, app, pilot, prefix):
        for _ in range(200):
            if any(line.startswith(prefix) for line in app.preview_lines):
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
            self.assertTrue(await self.wait_for_preview(app, pilot, "open:"))
            self.assertIn("open: w1", app.preview_lines)
            self.assertIn("label: repo", app.preview_lines)
            app.insert_text("rep")
            await pilot.pause()
            self.assertEqual([row.path for row in app.filtered], [self.repo])
            self.assertIn("open: w1", app.preview_lines)
            self.assertTrue(any("tabs:" in line for line in app.preview_lines))
            app.clear_query()
            await pilot.pause()
            self.assertIn("open: w1", app.preview_lines)

    async def test_preview_from_before_invalidation_is_not_cached(self):
        herdr = FakeHerdr(workspaces=[], panes=[])
        app = self.app(herdr)
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertTrue(await self.wait_for_preview(app, pilot, "open: no"))
            app.invalidate_previews()
            stale_generation = app._preview_generation - 1
            app._preview_loaded(self.repo, ["stale"], stale_generation)
            await pilot.pause()
            self.assertNotIn(self.repo, app._preview_cache)
            app._preview_loaded(self.repo, ["fresh"], app._preview_generation)
            await pilot.pause()
            self.assertEqual(app._preview_cache[self.repo], ["fresh"])


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
            mapping_reader=lambda: mapping,
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
            mapping_reader=lambda: {},
            src_pane="A",
        )

    async def test_extended_search_and_smartcase_keep_rank_order(self):
        app = self.app()
        async with app.run_test():
            app.insert_text("a | Beta")
            self.assertEqual([row.path for row in app.filtered], [self.alpha, self.beta, self.gamma])
            app.clear_query()
            app.insert_text("^/tmp")
            self.assertEqual(len(app.filtered), 3)
            app.clear_query()
            app.insert_text("gamma$")
            self.assertEqual([row.path for row in app.filtered], [self.gamma])
            app.clear_query()
            app.insert_text("!Beta")
            self.assertEqual([row.path for row in app.filtered], [self.alpha, self.gamma])
            app.clear_query()
            app.insert_text("Beta")
            self.assertEqual([row.path for row in app.filtered], [self.beta])

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
            mapping_reader=lambda: {},
            src_pane="A",
        )
        async with app.run_test():
            app.insert_text("^" + self.alpha + "$")
            self.assertEqual([row.path for row in app.filtered], [self.alpha])
            app.clear_query()
            app.insert_text("^" + self.alpha + "x$")
            self.assertEqual(app.filtered, [])
            app.clear_query()
            shown = next(row.shown for row in app.rows if row.path == self.beta)
            self.assertIn("[beta-label]", shown)
            app.insert_text("^" + shown + "$")
            self.assertEqual([row.path for row in app.filtered], [self.beta])
            app.clear_query()
            app.insert_text("[beta-label]$")
            self.assertEqual([row.path for row in app.filtered], [self.beta])

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
            mapping_reader=lambda: {"A": repo, "B": repo},
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
    """Serves one pane snapshot per main-thread ``panes()`` call."""

    def __init__(self, workspaces, snapshots):
        super().__init__(workspaces=workspaces, panes=snapshots[0])
        self.snapshots = list(snapshots)
        self.served = 0

    def panes(self):
        index = min(self.served, len(self.snapshots) - 1)
        if threading.current_thread() is threading.main_thread():
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
            mapping_reader=lambda: mapping if mapping is not None else {},
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

        def reader():
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
            mapping_reader=lambda: self.mapping,
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


if __name__ == "__main__":
    unittest.main()
