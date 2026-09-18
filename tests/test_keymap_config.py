import tempfile
import unittest
from pathlib import Path

from tools import keymap_config


class KeymapConfigTests(unittest.TestCase):
    def _load(self, text: str) -> keymap_config.KeymapConfig:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remap.toml"
            path.write_text(text, encoding="utf-8")
            return keymap_config.load_config(path)

    def test_loads_symbolic_japanese_layout(self):
        config = self._load(
            """format_version = 1

[remap]
caps_lock = "left_control"
left_control = "left_alt"
left_alt = "left_gui"
left_gui = "lang2"
right_gui = "lang1"
right_alt = "right_gui"
"""
        )

        self.assertEqual(config.remap["caps_lock"], 0xE0)
        self.assertEqual(config.remap["left_gui"], 0x91)
        self.assertEqual(config.remap["right_gui"], 0x90)

    def test_rejects_empty_remap(self):
        with self.assertRaisesRegex(keymap_config.KeymapConfigError, "empty"):
            self._load("format_version = 1\n\n[remap]\n")

    def test_rejects_unknown_physical_key(self):
        with self.assertRaisesRegex(keymap_config.KeymapConfigError, "physical key"):
            self._load('format_version = 1\n\n[remap]\nfn = "left_alt"\n')

    def test_rejects_unknown_hid_usage_name(self):
        with self.assertRaisesRegex(keymap_config.KeymapConfigError, "HID usage"):
            self._load('format_version = 1\n\n[remap]\ncaps_lock = "fn"\n')

    def test_rejects_wrong_format_version(self):
        with self.assertRaisesRegex(keymap_config.KeymapConfigError, "format_version"):
            self._load('format_version = 2\n\n[remap]\ncaps_lock = "left_control"\n')

    def test_rejects_duplicate_toml_key(self):
        with self.assertRaisesRegex(keymap_config.KeymapConfigError, "duplicate"):
            self._load(
                'format_version = 1\n\n[remap]\ncaps_lock = "left_control"\n'
                'caps_lock = "left_alt"\n'
            )


if __name__ == "__main__":
    unittest.main()
