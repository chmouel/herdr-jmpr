import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from herdr_zshz import cli


class FakeHerdr:
    """Writes a script that logs argv and serves a mutable fixture."""

    def __init__(self, root: Path, workspaces, panes, layout=None):
        self.root = root
        self.log = root / "calls.log"
        self.fixture = root / "fixture.json"
        self.layout = layout or {}
        self.fixture.write_text(
            json.dumps({"workspaces": workspaces, "panes": panes, "layout": self.layout})
        )
        self.executable = root / "herdr"
        self.executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['FAKE_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "data = json.load(open(os.environ['FAKE_FIXTURE']))\n"
            "if args[:2] == ['status', '--json']:\n"
            "    print(json.dumps({'server': {'socket': os.environ['FAKE_STATUS_SOCKET']}}))\n"
            "    sys.exit(0)\n"
            "if args[:2] == ['workspace', 'list']:\n"
            "    result = {'workspaces': data['workspaces']}\n"
            "elif args[:2] == ['pane', 'list']:\n"
            "    panes = data['panes']\n"
            "    if os.environ.get('FAKE_EXTRA_PANE'):\n"
            "        counter = os.environ['FAKE_LOG'] + '.count'\n"
            "        seen = int(open(counter).read()) if os.path.exists(counter) else 0\n"
            "        open(counter, 'w').write(str(seen + 1))\n"
            "        if seen >= 1:\n"
            "            panes = panes + [json.loads(os.environ['FAKE_EXTRA_PANE'])]\n"
            "    result = {'panes': panes}\n"
            "elif args[:2] == ['workspace', 'close']:\n"
            "    data['workspaces'] = [w for w in data['workspaces']"
            " if w['workspace_id'] != args[2]]\n"
            "    json.dump(data, open(os.environ['FAKE_FIXTURE'], 'w'))\n"
            "    result = {}\n"
            "elif args[:2] == ['workspace', 'create']:\n"
            "    result = {'workspace': {'workspace_id': 'w-new'}}\n"
            "elif args[:2] == ['workspace', 'focus']:\n"
            "    result = {}\n"
            "elif args[:2] == ['pane', 'layout']:\n"
            "    result = {'layout': data['layout']}\n"
            "elif args[:2] == ['tab', 'get']:\n"
            "    result = {'tab': {'tab_id': args[2], 'label': 'mytab'}}\n"
            "elif args[:2] == ['pane', 'move']:\n"
            "    if '--workspace' in args:\n"
            "        workspace = args[args.index('--workspace') + 1]\n"
            "    else:\n"
            "        workspace = 'w-moved'\n"
            "    result = {'move_result': {'changed': True, 'pane': {\n"
            "        'pane_id': 'new-' + args[2], 'tab_id': 't-new',\n"
            "        'workspace_id': workspace}}}\n"
            "else:\n"
            "    result = {}\n"
            "print(json.dumps({'result': result}))\n"
        )
        self.executable.chmod(0o755)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class CliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = self.root / "herdr.sock"
        self.socket.touch()
        self.target = self.root / "work" / "proj"
        self.target.mkdir(parents=True)
        (self.root / "state").mkdir()
        self.z_data = self.root / "z"
        self.z_data.write_text(f"{self.target}|10|{int(__import__('time').time())}\n")
        self.environment = {
            "HERDR_SOCKET_PATH": str(self.socket),
            "XDG_STATE_HOME": str(self.root / "state"),
            "ZSHZ_DATA": str(self.z_data),
            "FAKE_LOG": str(self.root / "calls.log"),
            "FAKE_FIXTURE": str(self.root / "fixture.json"),
            "HOME": str(self.root / "home"),
        }
        (self.root / "home").mkdir()

    def run_cli(self, arguments, stdin="", environment=None):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self.environment | (environment or {}), clear=False):
            with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stderr(stderr):
                code = cli.main(arguments)
        return code, stderr.getvalue()

    def fake(self, workspaces=None, panes=None, layout=None):
        return FakeHerdr(self.root, workspaces or [], panes or [], layout)

    def test_unknown_flag_prints_usage_and_exits_2(self):
        code, stderr = self.run_cli(["--bogus"])
        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.strip(),
            "usage: herdr-zshz [--move-pane|--move-tab] [--all] [--tabs|--no-tabs] [--backend <backend>]",
        )

    def test_backend_flag_validation_and_open_picker_forwarding(self):
        for missing_val in (["--backend"], ["--backend="]):
            with self.subTest(missing_val=missing_val):
                code, stderr = self.run_cli(missing_val)
                self.assertEqual(code, 2)
                self.assertIn("--backend", stderr)

        for unknown in (["--backend", "peco"], ["--backend=peco"]):
            with self.subTest(unknown=unknown):
                code, stderr = self.run_cli(unknown)
                self.assertEqual(code, 1)
                self.assertIn("Unknown backend 'peco'", stderr)

        fake = self.fake()
        code, _ = self.run_cli(
            ["open-picker", "--backend", "fzf"],
            environment={
                "HERDR_BIN_PATH": str(fake.executable),
                "HERDR_ACTIVE_PANE_ID": "A",
            },
        )
        self.assertEqual(code, 0)
        self.assertIn("HERDR_ZSHZ_BACKEND=fzf", fake.calls()[0])

    def test_open_picker_builds_command(self):
        fake = self.fake()
        code, _ = self.run_cli(
            ["open-picker", "--move-pane"],
            environment={
                "HERDR_BIN_PATH": str(fake.executable),
                "HERDR_ACTIVE_PANE_ID": "A",
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            fake.calls(),
            [
                [
                    "plugin",
                    "pane",
                    "open",
                    "--plugin",
                    "herdr-zshz",
                    "--entrypoint",
                    "move-pane",
                    "--placement",
                    "popup",
                    "--env",
                    "HERDR_ACTIVE_PANE_ID=A",
                ]
            ],
        )

    def test_open_picker_entrypoints(self):
        for arguments, expected in (
            ([], "jump"),
            (["--all"], "jump-all"),
            (["--move-tab"], "move-tab"),
        ):
            with self.subTest(arguments=arguments):
                fake = self.fake()
                fake.log.unlink(missing_ok=True)
                code, _ = self.run_cli(
                    ["open-picker", *arguments],
                    environment={"HERDR_BIN_PATH": str(fake.executable)},
                )
                self.assertEqual(code, 0)
                self.assertEqual(
                    fake.calls()[0][fake.calls()[0].index("--entrypoint") + 1], expected
                )

    def test_active_pane_from_context_json(self):
        context = json.dumps({"focused_pane": {"pane_id": "ctx-pane"}})
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": context}, clear=False):
            for key in ("HERDR_ACTIVE_PANE_ID", "HERDR_PANE_ID"):
                os.environ.pop(key, None)
            self.assertEqual(cli.active_pane_id(), "ctx-pane")
        context = json.dumps({"pane": {"pane_id": "pane-pane"}})
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": context}, clear=False):
            os.environ.pop("HERDR_ACTIVE_PANE_ID", None)
            os.environ.pop("HERDR_PANE_ID", None)
            self.assertEqual(cli.active_pane_id(), "pane-pane")

    def test_close_asks_before_closing_with_agent(self):
        workspaces = [{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}]
        panes = [{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t",
                  "cwd": str(self.target), "agent": "copilot"}]
        fake = self.fake(workspaces, panes)
        code, _ = self.run_cli(
            ["--close", str(self.target)],
            stdin="n",
            environment={"HERDR_BIN_PATH": str(fake.executable)},
        )
        self.assertEqual(code, 0)
        self.assertNotIn(["workspace", "close", "w1"], fake.calls())

        fake = self.fake(workspaces, panes)
        code, _ = self.run_cli(
            ["--close", str(self.target)],
            stdin="y",
            environment={"HERDR_BIN_PATH": str(fake.executable)},
        )
        self.assertEqual(code, 0)
        self.assertIn(["workspace", "close", "w1"], fake.calls())

    def test_close_without_agents_closes_directly(self):
        workspaces = [{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}]
        panes = [{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t", "cwd": str(self.target)}]
        fake = self.fake(workspaces, panes)
        git_rows = self.root / "git.rows"
        all_rows = self.root / "all.rows"
        (self.target / ".git").mkdir()
        git_rows.write_text("stale\n")
        all_rows.write_text("stale\n")
        code, _ = self.run_cli(
            ["--close", str(self.target)],
            environment={
                "HERDR_BIN_PATH": str(fake.executable),
                "HERDR_ZSHZ_GIT_ROWS": str(git_rows),
                "HERDR_ZSHZ_ALL_ROWS": str(all_rows),
            },
        )
        self.assertEqual(code, 0)
        self.assertIn(["workspace", "close", "w1"], fake.calls())
        self.assertIn(f"\t{self.target}", git_rows.read_text())
        self.assertNotIn("\x1b[34m●", git_rows.read_text())
        self.assertIn(f"\t{self.target}", all_rows.read_text())

    def test_close_aborts_when_agent_appears_in_phase_two(self):
        workspaces = [{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}]
        panes = [{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t", "cwd": str(self.target)}]
        fake = self.fake(workspaces, panes)
        extra = {
            "workspace_id": "w1",
            "pane_id": "p2",
            "tab_id": "t",
            "cwd": str(self.target),
            "agent": "copilot",
        }
        code, stderr = self.run_cli(
            ["--close", str(self.target)],
            environment={
                "HERDR_BIN_PATH": str(fake.executable),
                "FAKE_EXTRA_PANE": json.dumps(extra),
            },
        )
        self.assertEqual(code, 0)
        self.assertNotIn(["workspace", "close", "w1"], fake.calls())
        self.assertIn("started running there", stderr)

    def test_invalid_state_reports_mapping_error(self):
        fake = self.fake()
        state_path = (
            self.root / "state" / "herdr" / "plugins" / "state" / "herdr-zshz"
        )
        state_path.mkdir(parents=True)
        digest = hashlib.sha256(str(self.socket).encode()).hexdigest()
        (state_path / f"{digest}.json").write_text("{")
        code, stderr = self.run_cli(
            [], environment={"HERDR_BIN_PATH": str(fake.executable)}
        )
        self.assertEqual(code, 1)
        self.assertIn(
            f"Invalid workspace mapping: {state_path / f'{digest}.json'}", stderr
        )

    def test_close_refuses_invalid_canonical_mapping(self):
        workspaces = [{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}]
        panes = [{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t",
                  "cwd": str(self.target)}]
        fake = self.fake(workspaces, panes)
        state_path = (
            self.root / "state" / "herdr" / "plugins" / "state" / "herdr-zshz"
        )
        state_path.mkdir(parents=True)
        digest = hashlib.sha256(str(self.socket).encode()).hexdigest()
        broken = state_path / f"{digest}.json"
        broken.write_text("{")
        code, stderr = self.run_cli(
            ["--close", str(self.target)],
            stdin="y",
            environment={"HERDR_BIN_PATH": str(fake.executable)},
        )
        self.assertEqual(code, 1)
        self.assertIn(f"Invalid workspace mapping: {broken}", stderr)
        self.assertEqual(
            [call for call in fake.calls() if call[:2] == ["workspace", "close"]], []
        )

    def test_close_refuses_invalid_legacy_mapping(self):
        workspaces = [{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}]
        panes = [{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t",
                  "cwd": str(self.target)}]
        fake = self.fake(workspaces, panes)
        digest = hashlib.sha256(str(self.socket).encode()).hexdigest()
        legacy = self.root / "state" / "herdr" / "zsh-jump" / f"{digest}.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]")
        code, stderr = self.run_cli(
            ["--close", str(self.target)],
            stdin="y",
            environment={"HERDR_BIN_PATH": str(fake.executable)},
        )
        self.assertEqual(code, 1)
        self.assertIn(f"Invalid workspace mapping: {legacy}", stderr)
        self.assertEqual(
            [call for call in fake.calls() if call[:2] == ["workspace", "close"]], []
        )

    def test_close_resolves_socket_from_status_when_unset(self):
        workspaces = [{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}]
        panes = [{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t",
                  "cwd": str(self.target)}]
        fake = self.fake(workspaces, panes)
        code, stderr = self.run_cli(
            ["--close", str(self.target)],
            environment={
                "HERDR_BIN_PATH": str(fake.executable),
                "HERDR_SOCKET_PATH": "",
                "FAKE_STATUS_SOCKET": str(self.socket),
            },
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn(["workspace", "close", "w1"], fake.calls())

    def test_invalid_names_file_reports_error(self):
        config = self.root / "config"
        config.mkdir()
        (config / "names.tsv").write_text("bad line\n")
        fake = self.fake()
        code, stderr = self.run_cli(
            ["--close", str(self.target)],
            environment={
                "HERDR_BIN_PATH": str(fake.executable),
                "HERDR_PLUGIN_CONFIG_DIR": str(config),
            },
        )
        self.assertEqual(code, 1)
        self.assertIn("Invalid names.tsv line 1: bad line", stderr)

    def test_invalid_settings_file_reports_error(self):
        config = self.root / "config"
        config.mkdir()
        (config / "config.toml").write_text('start_mode = "vi"\n')
        fake = self.fake()
        code, stderr = self.run_cli(
            ["--close", str(self.target)],
            environment={
                "HERDR_BIN_PATH": str(fake.executable),
                "HERDR_PLUGIN_CONFIG_DIR": str(config),
            },
        )
        self.assertEqual(code, 1)
        self.assertIn("start_mode must be one of", stderr)


if __name__ == "__main__":
    unittest.main()


class JumpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = self.root / "herdr.sock"
        self.socket.touch()
        self.target = self.root / "work" / "proj"
        self.target.mkdir(parents=True)
        self.environment = {
            "HERDR_SOCKET_PATH": str(self.socket),
            "XDG_STATE_HOME": str(self.root / "state"),
            "HOME": str(self.root / "home"),
            "FAKE_LOG": str(self.root / "calls.log"),
            "FAKE_FIXTURE": str(self.root / "fixture.json"),
        }
        (self.root / "home").mkdir()

    def fake(self, workspaces=None, panes=None, layout=None):
        return FakeHerdr(self.root, workspaces or [], panes or [], layout)

    def run_jump(self, result, fake):
        from herdr_zshz import state
        from herdr_zshz.herdr import Herdr

        with mock.patch.dict(os.environ, self.environment | {"HERDR_BIN_PATH": str(fake.executable)}):
            instance = state.socket_instance(str(self.socket))
            path = state.state_file(str(self.socket))
            return cli.jump(result, Herdr(str(fake.executable)), str(self.socket), instance, path, {})

    def result(self, kind="jump", **kwargs):
        from herdr_zshz.tui import Result

        return Result(kind=kind, path=str(self.target), **kwargs)

    def mapping(self):
        from herdr_zshz import state

        with mock.patch.dict(os.environ, self.environment):
            return state.read_mapping(state.state_file(str(self.socket)))["workspaces"]

    def test_creates_workspace_and_records_mapping(self):
        fake = self.fake()
        self.assertEqual(self.run_jump(self.result(), fake), 0)
        calls = fake.calls()
        self.assertIn(
            ["workspace", "create", "--cwd", str(self.target), "--label", "proj", "--focus"], calls
        )
        self.assertEqual(calls[-1], ["workspace", "focus", "w-new"])
        self.assertEqual(self.mapping(), {"w-new": str(self.target)})

    def test_focuses_mapped_workspace(self):
        fake = self.fake(
            workspaces=[{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}],
        )
        from herdr_zshz import state

        with mock.patch.dict(os.environ, self.environment):
            path = state.state_file(str(self.socket))
            state.write_state(
                path, {"instance": state.socket_instance(str(self.socket)),
                       "workspaces": {"w1": str(self.target)}}
            )
        self.assertEqual(self.run_jump(self.result(), fake), 0)
        self.assertIn(["workspace", "focus", "w1"], fake.calls())
        self.assertFalse(any(call[:2] == ["workspace", "create"] for call in fake.calls()))

    def test_unmapped_pane_cwd_matches_tab(self):
        fake = self.fake(
            workspaces=[{"workspace_id": "w1", "label": "proj", "active_tab_id": "t2"}],
            panes=[
                {"workspace_id": "w1", "pane_id": "p1", "tab_id": "t1", "cwd": str(self.target)},
            ],
        )
        self.assertEqual(self.run_jump(self.result(), fake), 0)
        calls = fake.calls()
        self.assertIn(["workspace", "focus", "w1"], calls)
        self.assertIn(["tab", "focus", "t1"], calls)

    def test_concurrent_unknown_workspace_fails(self):
        fake = self.fake(
            workspaces=[{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}],
        )
        with self.assertRaises(cli.Failure) as raised:
            self.run_jump(self.result(), fake)
        self.assertEqual(
            str(raised.exception),
            "Workspace state changed concurrently for proj; please retry",
        )
        self.assertFalse(any(call[:2] == ["workspace", "focus"] for call in fake.calls()))

    def test_invalid_choice_fails(self):
        fake = self.fake(
            workspaces=[{"workspace_id": "w1", "label": "proj", "active_tab_id": "t"}],
        )
        with self.assertRaises(cli.Failure) as raised:
            self.run_jump(self.result(choice=("use", "gone")), fake)
        self.assertEqual(str(raised.exception), "Invalid workspace selection: gone")

    def test_move_pane_creates_workspace_without_create_call(self):
        fake = self.fake()
        layout = {
            "area": {"x": 0, "y": 0, "width": 100, "height": 50},
            "panes": [{"pane_id": "A", "rect": {"x": 0, "y": 0, "width": 100, "height": 50}}],
            "splits": [],
            "tab_id": "t-src",
            "workspace_id": "src",
        }
        result = self.result(
            kind="move-pane", src_pane="A", src_ws="src", src_layout=layout
        )
        self.assertEqual(self.run_jump(result, fake), 0)
        calls = fake.calls()
        self.assertIn(
            ["pane", "move", "A", "--new-workspace", "--label", "proj", "--no-focus"], calls
        )
        self.assertFalse(any(call[:2] == ["workspace", "create"] for call in calls))
        self.assertIn(["workspace", "focus", "w-moved"], calls)
        self.assertIn(["tab", "focus", "t-new"], calls)
        self.assertEqual(self.mapping(), {"w-moved": str(self.target)})

    def test_move_tab_replays_layout(self):
        layout = {
            "area": {"x": 0, "y": 0, "width": 100, "height": 50},
            "panes": [
                {"pane_id": "A", "rect": {"x": 0, "y": 0, "width": 60, "height": 50}},
                {"pane_id": "B", "rect": {"x": 60, "y": 0, "width": 40, "height": 50}},
            ],
            "splits": [
                {
                    "direction": "right",
                    "ratio": 0.6,
                    "rect": {"x": 0, "y": 0, "width": 100, "height": 50},
                }
            ],
            "tab_id": "t-src",
            "workspace_id": "src",
        }
        fake = self.fake(layout=layout)
        result = self.result(
            kind="move-tab",
            src_pane="A",
            src_ws="src",
            src_layout=layout,
            tab_label="mytab",
        )
        self.assertEqual(self.run_jump(result, fake), 0)
        moves = [call for call in fake.calls() if call[:2] == ["pane", "move"]]
        self.assertEqual(
            moves,
            [
                ["pane", "move", "A", "--new-workspace", "--label", "proj",
                 "--tab-label", "mytab", "--no-focus"],
                ["pane", "move", "B", "--tab", "t-new", "--split", "right",
                 "--target-pane", "new-A", "--ratio", "0.6", "--no-focus"],
            ],
        )
        self.assertIn(["workspace", "focus", "w-moved"], fake.calls())
        self.assertIn(["tab", "focus", "t-new"], fake.calls())

    def test_jump_focuses_target_tab_when_selected(self):
        fake = self.fake(
            workspaces=[{"workspace_id": "w1", "label": "proj", "active_tab_id": "t1"}]
        )
        result = self.result(
            kind="jump",
            target_workspace_id="w1",
            target_tab_id="t2",
        )
        self.assertEqual(self.run_jump(result, fake), 0)
        self.assertIn(["workspace", "focus", "w1"], fake.calls())
        self.assertIn(["tab", "focus", "t2"], fake.calls())
