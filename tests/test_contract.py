"""Contract checks against the Herdr that is installed on this machine.

They skip on machines and CI runners without Herdr, so `make check` stays
portable while a local `make test` catches upstream API drift.
"""

import json
import shutil
import subprocess
import unittest

HERDR = shutil.which("herdr")

SNAPSHOT_FIELDS = {
    "workspaces",
    "tabs",
    "panes",
    "layouts",
    "focused_pane_id",
    "focused_workspace_id",
}
LAYOUT_FIELDS = {"tab_id", "workspace_id", "area", "panes", "splits"}
WORKSPACE_FIELDS = {"workspace_id", "label", "tokens", "worktree"}
REPORT_METADATA_FIELDS = {"workspace_id", "source", "tokens"}
METHODS = {"session.snapshot", "workspace.report_metadata"}


def run_herdr(*arguments):
    return subprocess.run(
        [HERDR, *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


@unittest.skipUnless(HERDR, "herdr is not installed")
class SchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        completed = run_herdr("api", "schema", "--json")
        if completed.returncode != 0:
            raise unittest.SkipTest(
                "herdr api schema is unavailable: " + (completed.stderr or completed.stdout)
            )
        cls.schema = json.loads(completed.stdout)

    def definitions(self, name):
        return self.schema["schemas"][name].get("$defs") or {}

    def test_request_methods_exist(self):
        methods = set()
        for request in self.schema["schemas"]["request"]["oneOf"]:
            method = request["properties"]["method"]
            methods.update(method.get("enum") or [])
            if "const" in method:
                methods.add(method["const"])
        self.assertEqual(METHODS - methods, set())

    def test_snapshot_shape(self):
        snapshot = self.definitions("success_response").get("SessionSnapshot")
        self.assertIsNotNone(snapshot, "SessionSnapshot is missing from the schema")
        self.assertEqual(SNAPSHOT_FIELDS - set(snapshot["properties"]), set())

    def test_layout_shape(self):
        layout = self.definitions("success_response").get("PaneLayoutSnapshot")
        self.assertIsNotNone(layout, "PaneLayoutSnapshot is missing from the schema")
        self.assertEqual(LAYOUT_FIELDS - set(layout["properties"]), set())

    def test_workspace_shape(self):
        workspace = self.definitions("success_response").get("WorkspaceInfo")
        self.assertIsNotNone(workspace, "WorkspaceInfo is missing from the schema")
        self.assertEqual(WORKSPACE_FIELDS - set(workspace["properties"]), set())

    def test_report_metadata_params(self):
        params = self.definitions("request").get("WorkspaceReportMetadataParams")
        self.assertIsNotNone(params, "WorkspaceReportMetadataParams is missing")
        self.assertEqual(REPORT_METADATA_FIELDS - set(params["properties"]), set())


@unittest.skipUnless(HERDR, "herdr is not installed")
class LiveSnapshotTest(unittest.TestCase):
    def test_live_snapshot_has_the_keys_we_parse(self):
        completed = run_herdr("api", "snapshot")
        if completed.returncode != 0:
            self.skipTest("no running herdr server: " + (completed.stderr or completed.stdout))
        snapshot = json.loads(completed.stdout)["result"]["snapshot"]
        self.assertEqual(SNAPSHOT_FIELDS - set(snapshot), set())
        for layout in snapshot["layouts"]:
            self.assertEqual(LAYOUT_FIELDS - set(layout), set())
        for workspace in snapshot["workspaces"]:
            self.assertEqual(WORKSPACE_FIELDS - set(workspace), set())


if __name__ == "__main__":
    unittest.main()
