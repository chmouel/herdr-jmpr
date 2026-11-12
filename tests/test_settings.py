"""Settings tests: config.toml parsing and the start-mode default."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_zshz.settings import Settings, SettingsError, load_settings, settings_path


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"

    def write(self, text: str) -> None:
        self.path.write_text(text)

    def test_missing_file_keeps_the_built_in_default(self):
        self.assertEqual(load_settings(self.path), Settings())
        self.assertEqual(load_settings(self.path).start_mode, "insert")

    def test_start_mode_normal(self):
        self.write('start_mode = "normal"\n')
        self.assertEqual(load_settings(self.path).start_mode, "normal")

    def test_start_mode_insert_is_explicit_too(self):
        self.write('start_mode = "insert"\n')
        self.assertEqual(load_settings(self.path).start_mode, "insert")

    def test_invalid_start_mode_is_reported(self):
        self.write('start_mode = "vi"\n')
        with self.assertRaises(SettingsError) as caught:
            load_settings(self.path)
        self.assertIn("start_mode must be one of", str(caught.exception))

    def test_unknown_key_is_reported(self):
        self.write('keymap = "vim"\n')
        with self.assertRaises(SettingsError) as caught:
            load_settings(self.path)
        self.assertIn("unknown key 'keymap'", str(caught.exception))

    def test_malformed_toml_is_reported(self):
        self.write('start_mode = "normal\n')
        with self.assertRaises(SettingsError):
            load_settings(self.path)

    def test_path_follows_the_plugin_config_dir(self):
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": str(self.root)}):
            self.assertEqual(settings_path(), self.root / "config.toml")

    def test_path_falls_back_to_the_xdg_config_home(self):
        with mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.root)}, clear=False
        ), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
            self.assertEqual(
                settings_path(),
                self.root / "herdr" / "plugins" / "config" / "herdr-zshz" / "config.toml",
            )


if __name__ == "__main__":
    unittest.main()
