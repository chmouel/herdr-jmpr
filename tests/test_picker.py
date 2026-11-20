"""Tests for multi-backend picker resolution and fzf/skim/gum execution."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_zshz import picker
from herdr_zshz.herdr import Herdr


class StubHerdr(Herdr):
    def __init__(
        self,
        workspaces: list[dict] | None = None,
        panes: list[dict] | None = None,
        layout: dict | None = None,
        label: str = "main",
    ) -> None:
        super().__init__()
        self._workspaces = workspaces if workspaces is not None else []
        self._panes = panes if panes is not None else []
        self._layout = layout or {
            "area": {"x": 0, "y": 0, "width": 100, "height": 40},
            "panes": [{"pane_id": "p1", "rect": {"x": 0, "y": 0, "width": 100, "height": 40}}],
            "splits": [],
            "tab_id": "t1",
            "workspace_id": "w1",
        }
        self._label = label

    def workspaces(self) -> list[dict]:
        return list(self._workspaces)

    def panes(self) -> list[dict]:
        return list(self._panes)

    def pane_layout(self, pane_id: str) -> dict:
        return dict(self._layout)

    def tab_label(self, tab_id: str) -> str:
        return self._label


class BackendResolutionTest(unittest.TestCase):
    def test_default_and_precedence_cli_over_env_over_config(self):
        with mock.patch("herdr_zshz.picker.shutil.which", side_effect=lambda b: f"/usr/bin/{b}"):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(picker.BACKEND_ENV, None)
                self.assertEqual(picker.resolve_backend(), "textual")
                self.assertEqual(picker.resolve_backend("fzf"), "fzf")

            with mock.patch.dict(os.environ, {picker.BACKEND_ENV: "skim"}, clear=False):
                self.assertEqual(picker.resolve_backend("fzf", "gum"), "gum")
                self.assertEqual(picker.resolve_backend("fzf", None), "skim")

    def test_auto_probes_fzf_then_sk_then_gum_then_textual(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(picker.BACKEND_ENV, None)
            with mock.patch(
                "herdr_zshz.picker.shutil.which",
                side_effect=lambda name: "/bin/fzf" if name == "fzf" else None,
            ):
                self.assertEqual(picker.resolve_backend("auto"), "fzf")
            with mock.patch(
                "herdr_zshz.picker.shutil.which",
                side_effect=lambda name: "/bin/sk" if name == "sk" else None,
            ):
                self.assertEqual(picker.resolve_backend("auto"), "skim")
            with mock.patch(
                "herdr_zshz.picker.shutil.which",
                side_effect=lambda name: "/bin/gum" if name == "gum" else None,
            ):
                self.assertEqual(picker.resolve_backend("auto"), "gum")
            with mock.patch("herdr_zshz.picker.shutil.which", return_value=None):
                self.assertEqual(picker.resolve_backend("auto"), "textual")

    def test_explicit_missing_binary_and_unknown_backend_fail_clearly(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(picker.BACKEND_ENV, None)
            with mock.patch("herdr_zshz.picker.shutil.which", return_value=None):
                for name, binary in (("fzf", "fzf"), ("skim", "sk"), ("gum", "gum")):
                    with self.subTest(backend=name):
                        with self.assertRaises(picker.PickerError) as caught:
                            picker.resolve_backend("textual", name)
                        self.assertIn(binary, str(caught.exception))

            with self.assertRaises(picker.PickerError):
                picker.resolve_backend("textual", "peco")


class FormattingAndParsingTest(unittest.TestCase):
    def test_format_ansi_rows_marks_current_open_and_aliases(self):
        rows = picker.format_ansi_rows(
            ["/work/alpha", "/work/beta", "/work/gamma"],
            current="/work/alpha",
            opened={"/work/beta"},
            names={"/work/gamma": "Custom"},
        ).splitlines()
        self.assertTrue(rows[0].startswith("\x1b[33m\u25c6\x1b[0m "))
        self.assertTrue(rows[0].endswith("\t/work/alpha"))
        self.assertTrue(rows[1].startswith("\x1b[34m\u25cf\x1b[0m "))
        self.assertNotIn("[beta]", rows[1])
        self.assertIn("\x1b[2m[Custom]\x1b[0m\t/work/gamma", rows[2])

        plain = picker.format_ansi_rows(
            ["/work/gamma"],
            current=None,
            opened=set(),
            names={"/work/gamma": "Custom"},
            include_target=False,
        ).splitlines()
        self.assertNotIn("\t", plain[0])
        self.assertIn("[Custom]", picker.ANSI_RE.sub("", plain[0]))

    def test_parse_and_resolve_typed_or_picked(self):
        with tempfile.TemporaryDirectory() as temp:
            typed_dir = Path(temp) / "typed"
            typed_dir.mkdir()
            picked_dir = Path(temp) / "picked"
            picked_dir.mkdir()

            query, key, picked = picker.parse_query_expect_output(
                f"proj\nalt-p\n  ~/picked\t{picked_dir}\n"
            )
            self.assertEqual((query, key, picked), ("proj", "alt-p", str(picked_dir)))
            self.assertEqual(picker.resolve_typed_or_picked(query, picked), str(picked_dir))
            self.assertEqual(
                picker.resolve_typed_or_picked(str(typed_dir), str(picked_dir)),
                str(typed_dir.resolve()),
            )
            self.assertEqual(
                picker.resolve_typed_or_picked(str(typed_dir), ""),
                str(typed_dir.resolve()),
            )


class CliBackendsExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.extra = self.root / "extra"
        for directory in (self.alpha, self.beta, self.extra):
            directory.mkdir()

    def write_exec(self, name: str, body: str) -> Path:
        script = self.bin_dir / name
        script.write_text("#!/usr/bin/env python3\n" + body)
        script.chmod(0o755)
        return script

    def patch_path(self):
        path = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        return mock.patch.dict(os.environ, {"PATH": path}, clear=False)

    def test_run_fzf_passes_bindings_and_handles_alt_p_and_conflicts(self):
        calls_file = self.root / "fzf-calls.json"
        self.write_exec(
            "fzf",
            f"import json, os, sys\n"
            f"stdin_lines = sys.stdin.read().splitlines()\n"
            f"record = {{'argv': sys.argv[1:], 'stdin': stdin_lines, "
            f"'has_git_env': bool(os.environ.get({picker.GIT_ROWS_ENV!r})), "
            f"'has_all_env': bool(os.environ.get({picker.ALL_ROWS_ENV!r})), "
            f"'has_help_env': bool(os.environ.get({picker.HELP_ENV!r}))}}\n"
            f"prev = json.loads(open({str(calls_file)!r}).read()) if os.path.exists({str(calls_file)!r}) else []\n"
            f"prev.append(record)\n"
            f"open({str(calls_file)!r}, 'w').write(json.dumps(prev))\n"
            f"if '--expect' in sys.argv:\n"
            f"    print('beta')\n"
            f"    print('alt-p')\n"
            f"    print(stdin_lines[1])\n"
            f"else:\n"
            f"    print(stdin_lines[1])\n",
        )
        herdr = StubHerdr(
            workspaces=[{"workspace_id": "w-conflict", "label": "beta", "active_tab_id": "t9"}],
            panes=[
                {
                    "workspace_id": "w-conflict",
                    "pane_id": "p1",
                    "tab_id": "t1",
                    "cwd": str(self.alpha),
                }
            ],
        )
        with self.patch_path():
            result = picker.run_fzf(
                paths_git=[str(self.alpha), str(self.beta)],
                paths_all=[str(self.alpha), str(self.beta), str(self.extra)],
                current=str(self.alpha),
                opened={str(self.alpha)},
                names={},
                move_mode=None,
                show_all=False,
                src_pane="p1",
                herdr=herdr,
                mapping_reader=lambda: {},
            )
        assert result is not None
        self.assertEqual(result.path, str(self.beta))
        self.assertEqual(result.kind, "move-pane")
        self.assertEqual(result.choice, ("use", "w-conflict"))

        calls = json.loads(calls_file.read_text())
        self.assertEqual(len(calls), 2)
        first = calls[0]
        self.assertTrue(first["has_git_env"])
        self.assertTrue(first["has_all_env"])
        self.assertTrue(first["has_help_env"])
        self.assertIn("--highlight-line", first["argv"])
        self.assertIn("ctrl-h:toggle-preview", first["argv"])

    def test_run_fzf_accepts_typed_directory_on_exit_code_1(self):
        typed = self.root / "typed-only"
        typed.mkdir()
        self.write_exec(
            "fzf",
            f"import sys\n"
            f"sys.stdin.read()\n"
            f"print({str(typed)!r})\n"
            f"print('')\n"
            f"sys.exit(1)\n",
        )
        with self.patch_path():
            result = picker.run_fzf(
                paths_git=[str(self.alpha)],
                paths_all=[str(self.alpha)],
                current=None,
                opened=set(),
                names={},
                move_mode=None,
                show_all=False,
                src_pane="",
                herdr=StubHerdr(),
                mapping_reader=lambda: {},
            )
        assert result is not None
        self.assertEqual(result.path, str(typed.resolve()))
        self.assertEqual(result.kind, "jump")

    def test_run_skim_loops_on_alt_a_and_ctrl_x_then_returns_selection(self):
        step_file = self.root / "sk.step"
        self.write_exec(
            "sk",
            f"import os, sys\n"
            f"lines = sys.stdin.read().splitlines()\n"
            f"step = int(open({str(step_file)!r}).read()) if os.path.exists({str(step_file)!r}) else 0\n"
            f"open({str(step_file)!r}, 'w').write(str(step + 1))\n"
            f"if step == 0:\n"
            f"    assert 'git> ' in sys.argv\n"
            f"    print('ext')\n"
            f"    print('alt-a')\n"
            f"    print(lines[0])\n"
            f"elif step == 1:\n"
            f"    assert 'all> ' in sys.argv\n"
            f"    assert 'ext' in sys.argv\n"
            f"    print('ext')\n"
            f"    print('ctrl-x')\n"
            f"    print(lines[0])\n"
            f"else:\n"
            f"    print('ext')\n"
            f"    print('alt-t')\n"
            f"    print(lines[-1])\n",
        )
        closed: list[str] = []
        herdr = StubHerdr(
            panes=[
                {
                    "workspace_id": "w1",
                    "pane_id": "p1",
                    "tab_id": "t1",
                    "cwd": str(self.alpha),
                }
            ],
            label="feature",
        )
        with self.patch_path():
            result = picker.run_skim(
                paths_git=[str(self.alpha), str(self.beta)],
                paths_all=[str(self.alpha), str(self.beta), str(self.extra)],
                current=str(self.alpha),
                opened={str(self.alpha)},
                names={},
                move_mode=None,
                show_all=False,
                src_pane="p1",
                herdr=herdr,
                mapping_reader=lambda: {},
                history=[str(self.alpha), str(self.beta), str(self.extra)],
                close_callback=lambda target: closed.append(target) or 0,
            )
        self.assertEqual(closed, [str(self.alpha)])
        assert result is not None
        self.assertEqual(result.path, str(self.extra))
        self.assertEqual(result.kind, "move-tab")
        self.assertEqual(result.tab_label, "feature")
        self.assertIsNotNone(result.src_layout)

    def test_run_gum_maps_ansi_display_line_back_to_target(self):
        self.write_exec(
            "gum",
            "import sys\n"
            "assert sys.argv[1] == 'filter'\n"
            "assert '--no-strip-ansi' in sys.argv\n"
            "lines = sys.stdin.read().splitlines()\n"
            "print(lines[1])\n",
        )
        with self.patch_path():
            result = picker.run_gum(
                paths_git=[str(self.alpha), str(self.beta)],
                paths_all=[str(self.alpha), str(self.beta)],
                current=str(self.alpha),
                opened=set(),
                names={str(self.beta): "BetaProj"},
                move_mode=None,
                show_all=False,
                src_pane="",
                herdr=StubHerdr(),
                mapping_reader=lambda: {},
            )
        assert result is not None
        self.assertEqual(result.path, str(self.beta))


if __name__ == "__main__":
    unittest.main()
