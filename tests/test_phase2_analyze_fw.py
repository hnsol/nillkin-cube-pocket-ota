import struct
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path

from tools import phase2_analyze_fw as fw


def make_firmware(*, marker=b"MJNK", keymap=None, suffix=b""):
    if keymap is None:
        keymap = [0] * 130
        keymap[4] = 0x04
        keymap[44] = 0x2C
        keymap[120:128] = range(0xE0, 0xE8)
    return (
        b"HEAD-B077T_US_42\x00PAR2801\x001.0.0\x00"
        + marker
        + struct.pack("<130H", *keymap)
        + suffix
    )


class AnalyzeFirmwareTests(unittest.TestCase):
    def test_extracts_marker_and_little_endian_keymap(self):
        data = make_firmware()

        result = fw.analyze_bytes(data, source="sample.bin")

        self.assertEqual(result.marker_offset, 31)
        self.assertEqual(result.keymap_offset, 35)
        self.assertEqual(result.keymap_range, (35, 295))
        self.assertEqual(len(result.keymap), 130)
        self.assertEqual(result.keymap[4], 0x04)
        self.assertEqual(result.keymap[44], 0x2C)
        self.assertEqual(result.keymap[120:128], tuple(range(0xE0, 0xE8)))

    def test_reports_file_metadata_strings_and_hid_locations(self):
        result = fw.analyze_bytes(make_firmware(), source="sample.bin")

        self.assertEqual(result.source, "sample.bin")
        self.assertEqual(result.size, 295)
        self.assertEqual(
            result.sha256,
            "29811583feb7163c8ebb0183e5c15c87ccc25319e70d44bfc02d504df7414787",
        )
        self.assertEqual(result.sum16, 0x0F56)
        self.assertEqual(result.model_version_strings, ("B077T_US_42",))
        self.assertEqual(result.hardware_model_strings, ("PAR2801",))
        self.assertEqual(result.firmware_revision_strings, ("1.0.0",))
        self.assertEqual(result.hid_usages[0x04], (fw.HidLocation(4, 43),))
        self.assertEqual(result.hid_usages[0x2C], (fw.HidLocation(44, 123),))
        for modifier in range(0xE0, 0xE8):
            index = 120 + modifier - 0xE0
            self.assertEqual(
                result.hid_usages[modifier],
                (fw.HidLocation(index, 35 + 2 * index),),
            )

    def test_reports_conflicting_par_hardware_model_strings(self):
        result = fw.analyze_bytes(
            make_firmware(suffix=b"PAR2802"), source="sample.bin"
        )

        self.assertEqual(result.hardware_model_strings, ("PAR2801", "PAR2802"))

    def test_reports_caps_lock_location(self):
        keymap = [0] * 130
        keymap[13] = 0x39

        result = fw.analyze_bytes(
            make_firmware(keymap=keymap), source="sample.bin"
        )

        self.assertEqual(result.hid_usages[0x39], (fw.HidLocation(13, 61),))

    def test_rejects_missing_marker_with_clear_error(self):
        with self.assertRaisesRegex(fw.FirmwareAnalysisError, "MJNK marker.*not found"):
            fw.analyze_bytes(b"no marker here", source="missing.bin")

    def test_rejects_duplicate_marker_with_offsets(self):
        data = make_firmware(suffix=b"MJNK")

        with self.assertRaisesRegex(
            fw.FirmwareAnalysisError, r"multiple MJNK markers.*0x1f.*0x127"
        ):
            fw.analyze_bytes(data, source="duplicate.bin")

    def test_rejects_truncated_keymap_with_expected_and_available_sizes(self):
        data = b"MJNK" + b"\x00" * 10

        with self.assertRaisesRegex(
            fw.FirmwareAnalysisError, "keymap table is truncated.*260.*10"
        ):
            fw.analyze_bytes(data, source="short.bin")

    def test_analyze_file_reads_without_modifying_input(self):
        original = make_firmware()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.bin"
            path.write_bytes(original)

            result = fw.analyze_file(path)

            self.assertEqual(result.source, str(path))
            self.assertEqual(path.read_bytes(), original)


class ComparisonTests(unittest.TestCase):
    def test_reports_size_delta_and_keymap_element_differences(self):
        global_map = [0] * 130
        korean_map = global_map.copy()
        global_map[3] = 0xE2
        korean_map[3] = 0xE3
        global_result = fw.analyze_bytes(
            make_firmware(keymap=global_map), source="global.bin"
        )
        korean_result = fw.analyze_bytes(
            make_firmware(keymap=korean_map, suffix=b"KR"), source="kr.bin"
        )

        comparison = fw.compare_firmware(global_result, korean_result)

        self.assertEqual(comparison.size_delta, 2)
        self.assertEqual(
            comparison.keymap_differences,
            (
                fw.KeymapDifference(
                    index=3,
                    global_offset=41,
                    korean_offset=41,
                    global_value=0xE2,
                    korean_value=0xE3,
                ),
            ),
        )


class CliTests(unittest.TestCase):
    def test_two_paths_print_complete_analysis_and_comparison(self):
        global_map = [0] * 130
        global_map[4] = 0x04
        global_map[44] = 0x2C
        global_map[120:128] = range(0xE0, 0xE8)
        korean_map = global_map.copy()
        korean_map[121] = 0xE3
        with tempfile.TemporaryDirectory() as directory:
            global_path = Path(directory) / "global.bin"
            korean_path = Path(directory) / "kr.bin"
            global_path.write_bytes(make_firmware(keymap=global_map))
            korean_path.write_bytes(make_firmware(keymap=korean_map, suffix=b"KR"))
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = fw.main([str(global_path), str(korean_path)])

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("GLOBAL", output)
        self.assertIn("size: 295 bytes", output)
        self.assertIn("sha256: 29811583", output)
        self.assertIn("sum16: 0x0f56", output)
        self.assertIn("B077T_US_42", output)
        self.assertIn("PAR2801", output)
        self.assertIn("1.0.0", output)
        self.assertIn("MJNK offset: 0x1f", output)
        self.assertIn("keymap range: [0x23, 0x127)", output)
        self.assertIn("A (0x04): index 4, offset 0x2b", output)
        self.assertIn("Space (0x2c): index 44, offset 0x7b", output)
        self.assertIn("modifier 0xe0: index 120, offset 0x113", output)
        self.assertIn("file size delta (KR - GLOBAL): +2 bytes", output)
        self.assertIn(
            "index 121: GLOBAL 0xe1 @ 0x115 -> KR 0xe3 @ 0x115", output
        )


if __name__ == "__main__":
    unittest.main()
