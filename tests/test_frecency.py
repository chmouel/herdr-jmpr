import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_zshz import frecency
from herdr_zshz.frecency import (
    agents_by_path,
    current_project,
    data_path,
    display_path,
    git_root,
    open_paths,
    rank_paths,
    read_history,
    score,
)


class FrecencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = os.environ.get("HOME")

    def tearDown(self):
        if self.home is not None:
            os.environ["HOME"] = self.home

    def test_score_formula(self):
        now = 1_000_000
        expected = 10000 * 2 * (3.75 / ((0.0001 * 100 + 1) + 0.25))
        self.assertAlmostEqual(score(2, now - 100, now), expected)

    def test_data_path_precedence(self):
        old = {key: os.environ.get(key) for key in ("ZSHZ_DATA", "_Z_DATA", "XDG_CACHE_HOME")}
        try:
            os.environ["ZSHZ_DATA"] = "/tmp/zshz"
            os.environ["_Z_DATA"] = "/tmp/other"
            self.assertEqual(data_path(), Path("/tmp/zshz"))
            del os.environ["ZSHZ_DATA"]
            self.assertEqual(data_path(), Path("/tmp/other"))
            del os.environ["_Z_DATA"]
            os.environ["XDG_CACHE_HOME"] = str(self.root)
            (self.root / "zsh").mkdir()
            (self.root / "zsh" / "zsh-cache-z").write_text("")
            self.assertEqual(data_path(), self.root / "zsh" / "zsh-cache-z")
            (self.root / "zsh" / "zsh-cache-z").unlink()
            self.assertEqual(data_path(), Path.home() / ".z")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_read_history_orders_and_skips(self):
        now = 1_000_000
        path = self.root / "z"
        path.write_text(
            "/older|1|980000\n"
            "/newer|1|999000\n"
            "badline\n"
            "/nofields|1\n"
            "/nonnumeric|nope|1\n"
        )
        self.assertEqual(read_history(path, now), ["/newer", "/older"])

    def test_git_root(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "pkg").mkdir()
        self.assertEqual(git_root(str(repo / "pkg")), str(repo))
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(git_root(str(plain)))

    def test_rank_paths_buckets(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        other = self.root / "other"
        other.mkdir()
        open_dir = self.root / "open"
        open_dir.mkdir()
        missing = self.root / "missing"
        current = str(repo)
        history = [str(open_dir), str(missing), str(other), current]
        opened = {str(open_dir)}
        paths = rank_paths(history, current, opened, show_all=False)
        self.assertEqual(paths, [current, str(open_dir)])

    def test_rank_paths_all(self):
        plain = self.root / "plain"
        plain.mkdir()
        sub = plain / "project"
        sub.mkdir()
        paths = rank_paths([str(sub), str(plain)], None, set(), show_all=True)
        self.assertEqual(paths, [str(sub), str(plain)])

    def test_open_paths_and_current(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        pkg = repo / "pkg"
        pkg.mkdir()
        panes = [
            {"workspace_id": "w1", "pane_id": "p1", "tab_id": "t1", "cwd": str(pkg)},
            {"workspace_id": "w2", "pane_id": "p2", "tab_id": "t2", "cwd": "/elsewhere"},
        ]
        open_workspaces = [
            {"workspace_id": "w1", "label": "pkg", "active_tab_id": "t1"},
            {"workspace_id": "w2", "label": "repo", "active_tab_id": "t2"},
        ]
        self.assertEqual(open_paths(open_workspaces, panes, {}, {}), {str(pkg)})
        self.assertEqual(
            open_paths(open_workspaces, panes, {"w2": str(repo)}, {}),
            {str(pkg), str(repo)},
        )
        current_workspaces = [
            {"workspace_id": "w1", "label": "unrelated", "active_tab_id": "t1"},
            {"workspace_id": "w2", "label": "repo", "active_tab_id": "t2"},
        ]
        current = current_project("p1", panes, current_workspaces, {}, {})
        self.assertEqual(current, str(repo))

    def test_current_cleared_when_workspace_matches(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        workspaces = [{"workspace_id": "w1", "label": "repo", "active_tab_id": "t1"}]
        panes = [{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t1", "cwd": str(repo)}]
        self.assertIsNone(current_project("p1", panes, workspaces, {}, {}))
        self.assertIsNone(current_project("p1", panes, workspaces, {"w1": str(repo)}, {}))

    def test_current_skips_missing_pane(self):
        self.assertIsNone(current_project("nope", [], [], {}, {}))

    def test_display_path(self):
        os.environ["HOME"] = "/home/tester"
        self.assertEqual(display_path("/home/tester/repo"), "~/repo")
        self.assertEqual(display_path("/home/tester"), "~")
        self.assertEqual(display_path("/srv/repo"), "/srv/repo")

    def test_constant_marks(self):
        self.assertEqual(frecency.CURRENT_MARK, "◆")
        self.assertEqual(frecency.OPEN_MARK, "●")

    def test_read_history_zoxide_fallback_and_explicit_source(self):
        missing = self.root / "missing-z"
        fake_result = subprocess.CompletedProcess(
            ["zoxide", "query", "-l"], 0, stdout="/zox/one\n/zox/two\n", stderr=""
        )
        with (
            mock.patch.dict(os.environ, {"ZSHZ_DATA": str(missing)}),
            mock.patch("shutil.which", return_value="/usr/bin/zoxide"),
            mock.patch("subprocess.run", return_value=fake_result),
        ):
            self.assertEqual(read_history(source="auto"), ["/zox/one", "/zox/two"])
            self.assertEqual(read_history(source="zshz"), [])
            self.assertEqual(read_history(source="zoxide"), ["/zox/one", "/zox/two"])

    def test_agents_by_path_collects_mapped_and_unmapped_agents(self):
        workspaces = [
            {"workspace_id": "w1", "label": "repo"},
            {"workspace_id": "w2", "label": "app"},
        ]
        panes = [
            {"workspace_id": "w1", "cwd": "/tmp/repo/sub", "agent": "copilot"},
            {"workspace_id": "w1", "cwd": "/tmp/repo", "agent": "codex"},
            {"workspace_id": "w2", "cwd": "/tmp/app", "agent": "copilot"},
            {"workspace_id": "w2", "cwd": "/tmp/app", "agent": None},
        ]
        result = agents_by_path(workspaces, panes, {"w1": "/tmp/repo"}, {})
        self.assertEqual(
            result,
            {"/tmp/repo": ("codex", "copilot"), "/tmp/app": ("copilot",)},
        )


if __name__ == "__main__":
    unittest.main()
