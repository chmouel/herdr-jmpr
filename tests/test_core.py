import hashlib
import json
import multiprocessing
import os
import pty
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from herdr_zshz import cli
from herdr_zshz.config import NamesError, load_names, workspace_label
from herdr_zshz.frecency import current_project, open_paths, rank_paths
from herdr_zshz.state import legacy_state_dir, read_mapping, state_dir, state_file, write_state
from herdr_zshz.tui import Result


class FakeHerdr:
    def __init__(self, workspaces=None, panes=None):
        self._workspaces = list(workspaces or [])
        self._panes = list(panes or [])
        self.created = []
        self.focused = []
        self.tabs = []

    def workspaces(self):
        return list(self._workspaces)

    def panes(self):
        return list(self._panes)

    def workspace_create(self, cwd: str, label: str):
        workspace_id = f"w-{len(self._workspaces) + 1}"
        self.created.append((cwd, label))
        self._workspaces.append({"workspace_id": workspace_id, "label": label, "active_tab_id": "t"})
        return {"workspace_id": workspace_id}

    def workspace_focus(self, workspace_id: str):
        self.focused.append(workspace_id)

    def tab_focus(self, tab_id: str):
        self.tabs.append(tab_id)


class CoreBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = self.root / "herdr.sock"
        self.socket.touch()
        self.target = self.root / "repo" / "project"
        self.other = self.root / "repo" / "other"
        self.target.mkdir(parents=True)
        self.other.mkdir(parents=True)
        self.env = {
            "HERDR_SOCKET_PATH": str(self.socket),
            "XDG_STATE_HOME": str(self.root / "state"),
            "HOME": str(self.root / "home"),
        }
        (self.root / "home").mkdir()

    def run_jump(self, herdr, result, names=None, env_extra=None):
        names = names or {}
        with mock.patch.dict(os.environ, self.env | (env_extra or {}), clear=False):
            instance = f"{os.stat(self.socket).st_dev}:{os.stat(self.socket).st_ino}"
            path = state_file(str(self.socket))
            return cli.jump(result, herdr, str(self.socket), instance, path, names)

    def mapping(self, env_extra=None):
        with mock.patch.dict(os.environ, self.env | (env_extra or {}), clear=False):
            return read_mapping(state_file(str(self.socket)))["workspaces"]

    def test_mapping_reuse_and_unmapped_pane_matching(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "w1", "label": "project", "active_tab_id": "active"}],
            panes=[{"workspace_id": "w1", "pane_id": "p", "tab_id": "first", "cwd": str(self.target)}],
        )
        self.assertEqual(self.run_jump(herdr, Result(kind="jump", path=str(self.target))), 0)
        self.assertEqual(herdr.focused, ["w1"])
        self.assertEqual(herdr.tabs, ["first"])

        herdr2 = FakeHerdr(
            workspaces=[{"workspace_id": "w1", "label": "project", "active_tab_id": "active"}],
            panes=[],
        )
        self.assertEqual(self.run_jump(herdr2, Result(kind="jump", path=str(self.target))), 0)
        self.assertEqual(herdr2.created, [])
        self.assertEqual(herdr2.focused, ["w1"])

    def test_unknown_workspace_choice_and_cancel(self):
        herdr = FakeHerdr(
            workspaces=[{"workspace_id": "legacy", "label": "project", "active_tab_id": "t"}],
            panes=[],
        )
        with self.assertRaises(cli.Failure):
            self.run_jump(herdr, Result(kind="jump", path=str(self.target)))

        self.assertEqual(
            self.run_jump(
                herdr,
                Result(kind="jump", path=str(self.target), choice=("use", "legacy")),
            ),
            0,
        )
        self.assertEqual(herdr.focused[-1], "legacy")

    def test_frecency_markers_and_all_mode(self):
        workspaces = [{"workspace_id": "w1", "label": "project", "active_tab_id": "t1"}]
        panes = [{"workspace_id": "w1", "pane_id": "p1", "tab_id": "t1", "cwd": str(self.target)}]
        opened = open_paths(workspaces, panes, {}, {})
        current = current_project("p1", panes, workspaces, {}, {})
        ranked = rank_paths([str(self.other), str(self.target)], current, opened, False)
        self.assertEqual(ranked[0], str(self.target))
        ranked_all = rank_paths([str(self.other), str(self.target)], current, opened, True)
        self.assertIn(str(self.other), ranked_all)

    def test_names_tsv_parsing_and_labels(self):
        config_dir = self.root / "cfg"
        config_dir.mkdir()
        names_file = config_dir / "names.tsv"
        names_file.write_text(
            "# comment\n\n~/repo\tMy Repo\n" + f"{self.target}\tLeaf Label\n"
        )
        with mock.patch.dict(os.environ, self.env | {"HERDR_PLUGIN_CONFIG_DIR": str(config_dir)}, clear=False):
            names = load_names()
        self.assertEqual(names[str((self.root / 'home' / 'repo').resolve())], "My Repo")
        self.assertEqual(workspace_label(str(self.target / "pkg"), names), "Leaf Label")

        names_file.write_text("bad line\n")
        with self.assertRaises(NamesError):
            with mock.patch.dict(os.environ, self.env | {"HERDR_PLUGIN_CONFIG_DIR": str(config_dir)}, clear=False):
                load_names()

    def test_state_legacy_import_and_plugin_env_toggle(self):
        digest = hashlib.sha256(str(self.socket).encode()).hexdigest()
        with mock.patch.dict(os.environ, self.env, clear=False):
            canonical = state_dir() / f"{digest}.json"
            legacy = legacy_state_dir() / f"{digest}.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps({"instance": "old", "workspaces": {"w1": str(self.target)}}))
            self.assertEqual(read_mapping(canonical)["workspaces"], {"w1": str(self.target)})
            self.assertFalse(canonical.exists())

            canonical.parent.mkdir(parents=True, exist_ok=True)
            write_state(canonical, {"instance": "old", "workspaces": {"w2": str(self.other)}})
            self.assertEqual(read_mapping(canonical)["workspaces"], {"w2": str(self.other)})

        plugin_state = self.root / "state" / "herdr" / "plugins" / "state" / "herdr-zshz"
        env_with_plugin = self.env | {"HERDR_PLUGIN_STATE_DIR": str(plugin_state)}
        instance = f"{os.stat(self.socket).st_dev}:{os.stat(self.socket).st_ino}"
        plugin_file = plugin_state / f"{digest}.json"
        plugin_file.parent.mkdir(parents=True, exist_ok=True)
        write_state(plugin_file, {"instance": instance, "workspaces": {"w2": str(self.other)}})

        herdr = FakeHerdr(workspaces=[{"workspace_id": "w2", "label": "other", "active_tab_id": "t"}], panes=[])
        self.assertEqual(self.run_jump(herdr, Result(kind="jump", path=str(self.other)), env_extra=env_with_plugin), 0)

        herdr2 = FakeHerdr(workspaces=[{"workspace_id": "w2", "label": "other", "active_tab_id": "t"}], panes=[])
        self.assertEqual(self.run_jump(herdr2, Result(kind="jump", path=str(self.other))), 0)
        self.assertEqual(herdr2.created, [])


class LockConcurrencyTest(unittest.TestCase):
    def test_concurrent_jumps_do_not_clobber_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            socket = root / "herdr.sock"
            socket.touch()
            target_a = root / "A"
            target_b = root / "B"
            target_a.mkdir()
            target_b.mkdir()
            fake = root / "herdr"
            log = root / "calls.log"
            fixture = root / "fixture.json"
            fixture.write_text(json.dumps({"workspaces": []}))
            fake.write_text(
                "#!/usr/bin/env python3\n"  # noqa: UP031 - the script itself uses %r formatting
                "import json, os, sys, time\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['LOG'], 'a') as h: h.write(json.dumps(args)+'\\n')\n"
                "data = json.load(open(os.environ['FIXTURE']))\n"
                "if args[:2] == ['workspace','list']: out={'result':{'workspaces': data['workspaces']}}\n"
                "elif args[:2] == ['pane','list']: out={'result':{'panes':[]}}\n"
                "elif args[:2] == ['workspace','create']:\n"
                "  time.sleep(0.2)\n"
                "  cwd = args[args.index('--cwd')+1]\n"
                "  ws={'workspace_id':'w-'+os.path.basename(cwd), 'label': os.path.basename(cwd), 'active_tab_id':'t'}\n"
                "  data['workspaces'] = [w for w in data['workspaces'] if w['workspace_id'] != ws['workspace_id']] + [ws]\n"
                "  json.dump(data, open(os.environ['FIXTURE'], 'w'))\n"
                "  out={'result':{'workspace':{'workspace_id':ws['workspace_id']}}}\n"
                "else: out={'result':{}}\n"
                "print(json.dumps(out))\n"
            )
            fake.chmod(0o755)
            env = os.environ | {
                "HERDR_SOCKET_PATH": str(socket),
                "XDG_STATE_HOME": str(root / "state"),
                "HERDR_BIN_PATH": str(fake),
                "LOG": str(log),
                "HOME": str(root / "home"),
                "FIXTURE": str(fixture),
            }
            (root / "home").mkdir()

            def worker(path: str):
                from herdr_zshz import cli as _cli
                from herdr_zshz.herdr import Herdr as _Herdr
                from herdr_zshz.state import socket_instance as _instance
                from herdr_zshz.state import state_file as _state_file
                from herdr_zshz.tui import Result as _Result
                os.environ.update(env)
                _cli.jump(_Result(kind="jump", path=path), _Herdr(str(fake)), str(socket), _instance(str(socket)), _state_file(str(socket)), {})

            first = multiprocessing.Process(target=worker, args=(str(target_a),))
            second = multiprocessing.Process(target=worker, args=(str(target_b),))
            first.start()
            second.start()
            first.join(10)
            second.join(10)
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)

            with mock.patch.dict(os.environ, env, clear=False):
                mapping = read_mapping(state_file(str(socket)))["workspaces"]
            self.assertEqual(mapping, {"w-A": str(target_a), "w-B": str(target_b)})


class PtyHangupTest(unittest.TestCase):
    def test_move_tab_survives_sighup_and_saves_state(self):
        for backend in ("textual", "auto"):
            with self.subTest(backend=backend):
                self._run_hangup_case(backend)

    def _run_hangup_case(self, backend: str):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            socket = root / "herdr.sock"
            socket.touch()
            src = root / "src"
            dst = root / "dst"
            src.mkdir()
            dst.mkdir()
            (src / ".git").mkdir()
            (dst / ".git").mkdir()
            zdata = root / "z"
            zdata.write_text(f"{dst}|10|{int(time.time())}\n")
            fake = root / "herdr"
            log = root / "calls.log"
            count = root / "move.count"
            fake.write_text(
                "#!/usr/bin/env python3\n"  # noqa: UP031 - the script itself uses %r formatting
                "import json, os, signal, sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['LOG'], 'a') as h: h.write(json.dumps(args)+'\\n')\n"
                "layout={'area': {'x':0,'y':0,'width':100,'height':50}, 'panes':[{'pane_id':'A','rect':{'x':0,'y':0,'width':60,'height':50}},{'pane_id':'B','rect':{'x':60,'y':0,'width':40,'height':50}}], 'splits':[{'direction':'right','ratio':0.6,'rect':{'x':0,'y':0,'width':100,'height':50}}], 'tab_id':'t-src','workspace_id':'src'}\n"
                "if args[:2] == ['workspace','list']: out={'result':{'workspaces':[{'workspace_id':'src','label':'src','active_tab_id':'t-src'}]}}\n"
                "elif args[:2] == ['pane','list']: out={'result':{'panes':[{'workspace_id':'src','pane_id':'A','tab_id':'t-src','cwd':%r},{'workspace_id':'src','pane_id':'B','tab_id':'t-src','cwd':%r}]}}\n"
                "elif args[:2] == ['pane','layout']: out={'result':{'layout':layout}}\n"
                "elif args[:2] == ['tab','get']: out={'result':{'tab':{'tab_id':'t-src','label':'mytab'}}}\n"
                "elif args[:2] == ['pane','move']:\n"
                "  c = int(open(%r).read()) if os.path.exists(%r) else 0\n"
                "  open(%r, 'w').write(str(c+1))\n"
                "  if c == 0:\n"
                "    os.kill(os.getppid(), signal.SIGHUP)\n"
                "  workspace='w-dst' if '--workspace' not in args else args[args.index('--workspace')+1]\n"
                "  out={'result':{'move_result':{'changed':True,'pane':{'pane_id':'new-'+args[2],'tab_id':'t-new','workspace_id':workspace}}}}\n"
                "else: out={'result':{}}\n"
                "print(json.dumps(out))\n"
                % (str(src), str(src), str(count), str(count), str(count))
            )
            fake.chmod(0o755)

            env = os.environ | {
                "HERDR_SOCKET_PATH": str(socket),
                "XDG_STATE_HOME": str(root / "state"),
                "HERDR_BIN_PATH": str(fake),
                "HERDR_ACTIVE_PANE_ID": "A",
                "HERDR_ZSHZ_BACKEND": backend,
                "ZSHZ_DATA": str(zdata),
                "HOME": str(root / "home"),
                "LOG": str(log),
            }
            (root / "home").mkdir()
            master, slave = pty.openpty()
            try:
                proc = subprocess.Popen(
                    [str(Path(__file__).resolve().parents[1] / "bin" / "herdr-zshz"), "--move-tab"],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    start_new_session=True,
                    text=False,
                )
                time.sleep(0.8)
                os.write(master, b"\r")
                # The picker writes a full screen per refresh; with no reader
                # the pty buffer fills and the child blocks before it can
                # consume the Enter above. Drain it while waiting.
                os.set_blocking(master, False)
                deadline = time.time() + 20
                code = None
                while time.time() < deadline:
                    try:
                        while os.read(master, 65536):
                            pass
                    except (BlockingIOError, OSError):
                        pass
                    code = proc.poll()
                    if code is not None:
                        break
                    time.sleep(0.02)
                if code is None:
                    proc.kill()
                    proc.wait()
                    self.fail("herdr-zshz did not exit after accepting the move")
            finally:
                os.close(slave)
                os.close(master)
            self.assertEqual(code, 0)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            moves = [call for call in calls if call[:2] == ["pane", "move"]]
            self.assertEqual(len(moves), 2)
            self.assertIn(["workspace", "focus", "w-dst"], calls)
            self.assertIn(["tab", "focus", "t-new"], calls)
            with mock.patch.dict(os.environ, env, clear=False):
                mapping = read_mapping(state_file(str(socket)))["workspaces"]
            self.assertIn("w-dst", mapping)


if __name__ == "__main__":
    unittest.main()
