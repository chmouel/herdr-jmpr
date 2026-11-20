import os
import tempfile
import unittest
from pathlib import Path

from herdr_jmpr.config import NamesError, load_names, names_path, normalize_path, workspace_label


class NamesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, content):
        path = self.root / "names.tsv"
        path.write_text(content)
        return path

    def test_missing_file_is_empty(self):
        self.assertEqual(load_names(self.root / "absent.tsv"), {})

    def test_parses_comments_blanks_and_tabs(self):
        path = self.write("# comment\n\n/a/b\tMy Repo\n/c\tThird\n")
        self.assertEqual(load_names(path), {"/a/b": "My Repo", "/c": "Third"})

    def test_invalid_line_reports_lineno(self):
        path = self.write("/a/b\tTwo\tTabs\n")
        with self.assertRaises(NamesError) as raised:
            load_names(path)
        self.assertEqual(str(raised.exception), "Invalid names.tsv line 1: /a/b\tTwo\tTabs")

    def test_empty_path_or_label_is_invalid(self):
        for line in ("\tlabel", "/path\t", "/path"):
            with self.subTest(line=line):
                path = self.write(line + "\n")
                with self.assertRaises(NamesError):
                    load_names(path)

    def test_expands_home_and_trailing_slashes(self):
        home = os.environ["HOME"]
        path = self.write(f"{home}/repo/\tRepo\n")
        self.assertEqual(load_names(path), {f"{home}/repo": "Repo"})

    def test_names_path_precedence(self):
        old_config = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        try:
            os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(self.root / "cfg")
            self.assertEqual(names_path(), self.root / "cfg" / "names.tsv")
            del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
            os.environ["XDG_CONFIG_HOME"] = str(self.root / "xdg")
            self.assertEqual(
                names_path(),
                self.root / "xdg" / "herdr" / "plugins" / "config" / "herdr-jmpr" / "names.tsv",
            )
        finally:
            for key, value in (
                ("HERDR_PLUGIN_CONFIG_DIR", old_config),
                ("XDG_CONFIG_HOME", old_xdg),
            ):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_normalize_path(self):
        self.assertEqual(normalize_path("~"), os.environ["HOME"])
        self.assertEqual(normalize_path("/a/b///"), "/a/b")
        self.assertEqual(normalize_path("/"), "/")


class LabelTest(unittest.TestCase):
    def test_walks_up_to_nearest_name(self):
        names = {"/a": "A", "/a/b": "B"}
        self.assertEqual(workspace_label("/a/b/c", names), "B")
        self.assertEqual(workspace_label("/a/x", names), "A")

    def test_falls_back_to_basename(self):
        self.assertEqual(workspace_label("/a/b/c", {}), "c")
        self.assertEqual(workspace_label("/", {}), "/")

    def test_matches_after_normalising(self):
        self.assertEqual(workspace_label("/a/b/", {"/a/b": "Bee"}), "Bee")


if __name__ == "__main__":
    unittest.main()
