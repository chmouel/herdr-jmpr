import json
import tempfile
import unittest
from pathlib import Path

from herdr_jmpr.herdr import Herdr, HerdrError, layout_ops


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
        "workspace_id": workspace_id,
    }


class LayoutOpsTest(unittest.TestCase):
    def test_single_pane(self):
        self.assertEqual(layout_ops(layout("w", ["A"])), ["A"])

    def test_side_by_side(self):
        self.assertEqual(layout_ops(layout("w", ["A", "B"])), ["A", "B\tA\tright\t0.6"])

    def test_nested(self):
        ops = layout_ops(layout("w", ["A", "B", "C"]))
        self.assertEqual(ops[0], "A")
        self.assertEqual(ops[1], "B\tA\tright\t0.6")
        self.assertEqual(ops[2], "C\tB\tright\t0.6")

    def test_unmappable_layout_raises(self):
        broken = layout("w", ["A"])
        broken["panes"] = []
        with self.assertRaises(HerdrError):
            layout_ops(broken)


class ScriptedHerdrTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def script(self, body):
        path = self.root / "herdr"
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o755)
        return str(path)

    def test_run_returns_json_and_forwards_args(self):
        args_file = self.root / "args"
        executable = self.script(
            "import json, sys\n"
            f"open({str(args_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "print(json.dumps({'result': {'snapshot': {'workspaces': []}}}))\n"
        )
        self.assertEqual(Herdr(executable).workspaces(), [])
        self.assertEqual(json.loads(args_file.read_text()), ["api", "snapshot"])

    def test_run_failure_raises(self):
        executable = self.script("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
        with self.assertRaises(HerdrError) as raised:
            Herdr(executable).workspaces()
        self.assertIn("boom", str(raised.exception))

    def test_pane_move_refusal_raises(self):
        executable = self.script(
            "import json\nprint(json.dumps({'result': {'move_result': {'changed': False}}}))\n"
        )
        with self.assertRaises(HerdrError):
            Herdr(executable).pane_move("A", "--new-tab", "--workspace", "w1")

    def test_pane_move_returns_pane(self):
        executable = self.script(
            "import json\nprint(json.dumps({'result': {'move_result': {'changed': True,"
            " 'pane': {'pane_id': 'new', 'tab_id': 't', 'workspace_id': 'w'}}}}))\n"
        )
        self.assertEqual(
            Herdr(executable).pane_move("A", "--new-workspace"),
            {"pane_id": "new", "tab_id": "t", "workspace_id": "w"},
        )

    def test_workspace_create_requires_id(self):
        executable = self.script("import json\nprint(json.dumps({'result': {}}))\n")
        with self.assertRaises(HerdrError):
            Herdr(executable).workspace_create("/tmp", "label")

    def test_missing_binary_raises(self):
        with self.assertRaises(HerdrError):
            Herdr(str(self.root / "absent")).workspaces()


class StatusSocketTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def script(self, payload):
        path = self.root / "herdr"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"print(json.dumps({payload}))\n"
        )
        path.chmod(0o755)
        return str(path)

    def test_top_level_server_socket(self):
        executable = self.script("{'server': {'socket': '/tmp/live.sock'}}")
        self.assertEqual(Herdr(executable).status_socket(), "/tmp/live.sock")

    def test_result_envelope_is_still_accepted(self):
        executable = self.script("{'result': {'server': {'socket': '/tmp/wrapped.sock'}}}")
        self.assertEqual(Herdr(executable).status_socket(), "/tmp/wrapped.sock")

    def test_malformed_response_raises(self):
        executable = self.script("{'server': {}}")
        with self.assertRaises(HerdrError) as raised:
            Herdr(executable).status_socket()
        self.assertIn("server.socket", str(raised.exception))



class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def script(self, body):
        path = self.root / "herdr"
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o755)
        return str(path)

    def payload(self):
        return {
            "result": {
                "snapshot": {
                    "workspaces": [{"workspace_id": "w1", "label": "proj"}],
                    "tabs": [{"tab_id": "t1", "workspace_id": "w1", "label": "main"}],
                    "panes": [
                        {"pane_id": "p1", "tab_id": "t1", "workspace_id": "w1"},
                    ],
                    "layouts": [
                        {
                            "tab_id": "t1",
                            "workspace_id": "w1",
                            "area": {"x": 0, "y": 0, "width": 80, "height": 24},
                            "panes": [
                                {
                                    "pane_id": "p1",
                                    "rect": {"x": 0, "y": 0, "width": 80, "height": 24},
                                }
                            ],
                            "splits": [],
                        }
                    ],
                }
            }
        }

    def test_snapshot_reads_one_call(self):
        args_file = self.root / "args"
        executable = self.script(
            "import json, sys\n"
            f"open({str(args_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            f"print(json.dumps({self.payload()!r}))\n"
        )
        snapshot = Herdr(executable).snapshot()
        self.assertEqual(json.loads(args_file.read_text()), ["api", "snapshot"])
        self.assertEqual(snapshot["workspaces"][0]["workspace_id"], "w1")
        self.assertEqual(snapshot["tabs"][0]["label"], "main")

    def test_snapshot_without_payload_raises(self):
        executable = self.script("import json\nprint(json.dumps({'result': {}}))\n")
        with self.assertRaises(HerdrError):
            Herdr(executable).snapshot()

    def test_layout_and_label_come_from_the_snapshot(self):
        executable = self.script(
            "import json\n" f"print(json.dumps({self.payload()!r}))\n"
        )
        herdr = Herdr(executable)
        self.assertEqual(herdr.pane_layout("p1")["tab_id"], "t1")
        self.assertEqual(herdr.tab_label("t1"), "main")

    def test_unknown_pane_raises(self):
        executable = self.script(
            "import json\n" f"print(json.dumps({self.payload()!r}))\n"
        )
        with self.assertRaises(HerdrError):
            Herdr(executable).pane_layout("absent")

    def test_report_workspace_path_forwards_the_token(self):
        args_file = self.root / "args"
        executable = self.script(
            "import json, sys\n"
            f"open({str(args_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "print(json.dumps({'result': {}}))\n"
        )
        Herdr(executable).report_workspace_path("w1", "/src/proj")
        self.assertEqual(
            json.loads(args_file.read_text()),
            [
                "workspace",
                "report-metadata",
                "w1",
                "--source",
                "herdr-jmpr",
                "--token",
                "jmpr_path=/src/proj",
            ],
        )


if __name__ == "__main__":
    unittest.main()
