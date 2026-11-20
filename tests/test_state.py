import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from herdr_jmpr.state import (
    StateError,
    legacy_state_dir,
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

SOCKET = "/tmp/herdr.sock"


class StateDirTest(unittest.TestCase):
    def setUp(self):
        self.saved = {
            key: os.environ.get(key)
            for key in ("HERDR_PLUGIN_STATE_DIR", "XDG_STATE_HOME", "HOME")
        }
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_plugin_state_dir_wins(self):
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(self.root / "plugin")
        os.environ["XDG_STATE_HOME"] = str(self.root / "xdg")
        self.assertEqual(state_dir(), self.root / "plugin")
        self.assertEqual(legacy_state_dir(), self.root / "xdg" / "herdr" / "zsh-jump")

    def test_xdg_state_home_fallback(self):
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        os.environ["XDG_STATE_HOME"] = str(self.root / "xdg")
        self.assertEqual(
            state_dir(), self.root / "xdg" / "herdr" / "plugins" / "state" / "herdr-jmpr"
        )

    def test_empty_plugin_state_dir_is_ignored(self):
        os.environ["HERDR_PLUGIN_STATE_DIR"] = ""
        os.environ["XDG_STATE_HOME"] = str(self.root / "xdg")
        self.assertEqual(
            state_dir(), self.root / "xdg" / "herdr" / "plugins" / "state" / "herdr-jmpr"
        )

    def test_state_and_lock_file_names(self):
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(self.root / "plugin")
        digest = hashlib.sha256(SOCKET.encode()).hexdigest()
        self.assertEqual(state_file(SOCKET), self.root / "plugin" / f"{digest}.json")
        self.assertEqual(lock_file(SOCKET), self.root / "plugin" / f"{digest}.json.lock")

    def test_socket_instance_follows_symlinks(self):
        target = self.root / "real.sock"
        target.touch()
        link = self.root / "link.sock"
        link.symlink_to(target)
        stat = os.stat(target)
        self.assertEqual(socket_instance(str(link)), f"{stat.st_dev}:{stat.st_ino}")


class MappingFileTest(unittest.TestCase):
    def setUp(self):
        self.saved = {
            key: os.environ.get(key)
            for key in ("HERDR_PLUGIN_STATE_DIR", "XDG_STATE_HOME")
        }
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")
        self.path = state_file(SOCKET)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def legacy(self, payload):
        legacy = legacy_state_dir() / self.path.name
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(payload)
        return legacy

    def test_missing_state_is_empty(self):
        self.assertEqual(
            read_mapping(self.path), {"instance": "", "workspaces": {}}
        )

    def test_reads_canonical(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"instance": "1:2", "workspaces": {"w1": "/a"}}))
        self.assertEqual(read_mapping(self.path)["workspaces"], {"w1": "/a"})

    def test_imports_legacy_without_touching_it(self):
        legacy = self.legacy(json.dumps({"instance": "1:2", "workspaces": {"w": "/a"}}))
        before = legacy.read_text()
        self.assertEqual(read_mapping(self.path)["workspaces"], {"w": "/a"})
        self.assertFalse(self.path.exists())
        self.assertEqual(legacy.read_text(), before)

    def test_invalid_canonical_raises_with_path(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertRaises(StateError) as raised:
            read_mapping(self.path)
        self.assertEqual(raised.exception.path, self.path)

    def test_invalid_schema_raises(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"instance": 1, "workspaces": {}}))
        with self.assertRaises(StateError):
            read_mapping(self.path)

    def test_invalid_legacy_raises_with_legacy_path(self):
        legacy = self.legacy("[]")
        with self.assertRaises(StateError) as raised:
            read_mapping(self.path)
        self.assertEqual(raised.exception.path, legacy)

    def test_live_mapping_ignores_other_instance(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"instance": "old", "workspaces": {"w": "/a"}}))
        self.assertEqual(read_live_mapping(self.path, "new"), {})
        self.assertEqual(read_live_mapping(self.path, "old"), {"w": "/a"})

    def test_strict_live_mapping_raises_on_invalid_canonical(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertRaises(StateError) as raised:
            read_live_mapping_strict(self.path, "1:2")
        self.assertEqual(raised.exception.path, self.path)

    def test_strict_live_mapping_raises_on_invalid_legacy(self):
        legacy = self.legacy("[]")
        with self.assertRaises(StateError) as raised:
            read_live_mapping_strict(self.path, "1:2")
        self.assertEqual(raised.exception.path, legacy)

    def test_strict_live_mapping_matches_tolerant_reader(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"instance": "1:2", "workspaces": {"w": "/a"}}))
        self.assertEqual(read_live_mapping_strict(self.path, "1:2"), {"w": "/a"})
        self.assertEqual(read_live_mapping_strict(self.path, "3:4"), {})

    def test_write_state_is_atomic_and_readable(self):
        self.path.parent.mkdir(parents=True)
        write_state(self.path, {"instance": "1:2", "workspaces": {"w": "/a"}})
        self.assertEqual(read_mapping(self.path)["workspaces"], {"w": "/a"})
        self.assertEqual(
            [entry.name for entry in self.path.parent.iterdir()], [self.path.name]
        )


class RecoverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = str(self.root / "herdr.sock")
        Path(self.socket).touch()

    def test_prunes_dead_workspaces(self):
        mapping = {"instance": "same", "workspaces": {"w1": "/a", "w2": "/b"}}
        recovered = recover(mapping, self.socket, "same", {"w1"})
        self.assertEqual(recovered, {"instance": "same", "workspaces": {"w1": "/a"}})

    def test_restart_keeps_session_matches(self):
        (self.root / "session.json").write_text(
            json.dumps({"workspaces": [{"id": "w1", "identity_cwd": "/a"}]})
        )
        mapping = {"instance": "old", "workspaces": {"w1": "/a", "w2": "/b"}}
        recovered = recover(mapping, self.socket, "new", {"w1", "w2"})
        self.assertEqual(recovered, {"instance": "new", "workspaces": {"w1": "/a"}})

    def test_restart_without_session_clears(self):
        mapping = {"instance": "old", "workspaces": {"w1": "/a"}}
        recovered = recover(mapping, self.socket, "new", {"w1"})
        self.assertEqual(recovered, {"instance": "new", "workspaces": {}})

    def test_restart_with_broken_session_clears(self):
        (self.root / "session.json").write_text("{")
        mapping = {"instance": "old", "workspaces": {"w1": "/a"}}
        self.assertEqual(
            recover(mapping, self.socket, "new", {"w1"})["workspaces"], {}
        )


class CloseTargetsTest(unittest.TestCase):
    def test_mapped_and_unmapped_targets(self):
        workspaces = [
            {"workspace_id": "w1", "label": "proj"},
            {"workspace_id": "w2", "label": "proj"},
            {"workspace_id": "w3", "label": "other"},
        ]
        panes = [
            {"workspace_id": "w1", "pane_id": "p1", "agent": "copilot"},
            {"workspace_id": "w2", "pane_id": "p2", "cwd": "/work/proj"},
            {"workspace_id": "w3", "pane_id": "p3", "cwd": "/work/proj"},
        ]
        close_ids, agents, names = snapshot_close_targets(
            "/work/proj", workspaces, panes, {"w1": "/work/proj"}, {}
        )
        self.assertEqual(close_ids, ["w1", "w2"])
        self.assertEqual(agents, {("w1", "p1", "copilot")})
        self.assertEqual(names, ["copilot"])

    def test_mapped_elsewhere_is_not_closed(self):
        workspaces = [{"workspace_id": "w1", "label": "proj"}]
        close_ids, agents, names = snapshot_close_targets(
            "/work/proj", workspaces, [], {"w1": "/elsewhere"}, {}
        )
        self.assertEqual((close_ids, agents, names), ([], set(), []))

    def test_agents_are_unique_and_sorted(self):
        workspaces = [{"workspace_id": "w1", "label": "proj"}]
        panes = [
            {"workspace_id": "w1", "pane_id": "p1", "agent": "zed"},
            {"workspace_id": "w1", "pane_id": "p2", "agent": "copilot"},
            {"workspace_id": "w1", "pane_id": "p3", "agent": None},
        ]
        close_ids, agents, names = snapshot_close_targets(
            "/work/proj", workspaces, panes, {"w1": "/work/proj"}, {}
        )
        self.assertEqual(len(agents), 2)
        self.assertEqual(names, ["copilot", "zed"])

    def test_worktree_workspace_is_matched_regardless_of_label(self):
        workspaces = [
            {
                "workspace_id": "w9A",
                "label": "#515 [Feature]: Allow local edits",
                "worktree": {"checkout_path": "/work/wt/issue-515"},
            }
        ]
        panes = [{"workspace_id": "w9A", "pane_id": "p1", "agent": "pi"}]
        close_ids, agents, names = snapshot_close_targets(
            "/work/wt/issue-515", workspaces, panes, {}, {}
        )
        self.assertEqual(close_ids, ["w9A"])
        self.assertEqual(agents, {("w9A", "p1", "pi")})
        self.assertEqual(names, ["pi"])


if __name__ == "__main__":
    unittest.main()


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.saved = {
            key: os.environ.get(key)
            for key in ("HERDR_PLUGIN_STATE_DIR", "XDG_STATE_HOME")
        }
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")
        self.path = state_file(SOCKET)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_legacy_import_is_read_only(self):
        legacy = legacy_state_dir() / self.path.name
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"instance": "1:2", "workspaces": {"w": "/a"}}))
        self.assertEqual(read_mapping(self.path)["workspaces"], {"w": "/a"})
        self.assertFalse(self.path.exists())
        self.assertTrue(legacy.exists())

    def test_canonical_wins_over_legacy(self):
        legacy = legacy_state_dir() / self.path.name
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"instance": "1:2", "workspaces": {"w": "/old"}}))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"instance": "1:2", "workspaces": {"w": "/new"}}))
        self.assertEqual(read_mapping(self.path)["workspaces"], {"w": "/new"})

    def test_invalid_legacy_raises_with_legacy_path(self):
        legacy = legacy_state_dir() / self.path.name
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]")
        with self.assertRaises(StateError) as raised:
            read_mapping(self.path)
        self.assertEqual(raised.exception.path, legacy)
