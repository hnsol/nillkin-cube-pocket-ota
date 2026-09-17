import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from tools import firmware_image as images
from tools import phase3_build_patch as fw


FIXTURE = bytearray(b"\x11" * 96)
FIXTURE[8:19] = b"B077T_US_13"
FIXTURE[40] = 0x39
FIXTURE[42] = 0xE0
FIXTURE[44] = 0xE2
FIXTURE[46] = 0xE3
FIXTURE[48] = 0xE7
FIXTURE[50] = 0xE6
FIXTURE = bytes(FIXTURE)
FIXTURE_SHA256 = "562eff55f17ff7dac54f80b4917787d0756e84380d46c4c451f1eb44ef6a2d6d"
FIXTURE_PATCHES = (
    fw.BytePatch(40, 0x39, 0xE0, "Caps"),
    fw.BytePatch(42, 0xE0, 0xE2, "Left Ctrl"),
    fw.BytePatch(44, 0xE2, 0xE3, "Left Alt"),
    fw.BytePatch(46, 0xE3, 0x91, "Left Cmd"),
    fw.BytePatch(48, 0xE7, 0x90, "Right Cmd"),
    fw.BytePatch(50, 0xE6, 0xE7, "Right Alt"),
)
FIXTURE_SPEC = fw.FirmwareSpec(
    size=96,
    sha256=FIXTURE_SHA256,
    version=b"B077T_US_13",
    patches=FIXTURE_PATCHES,
    patched_sum16=0x0CEA,
)


class ValidationTests(unittest.TestCase):
    def test_accepts_exact_expected_original(self):
        fw.validate_original(FIXTURE, FIXTURE_SPEC)

    def test_rejects_wrong_size(self):
        with self.assertRaisesRegex(fw.FirmwarePatchError, "size"):
            fw.validate_original(FIXTURE[:-1], FIXTURE_SPEC)

    def test_rejects_wrong_hash(self):
        corrupted = bytearray(FIXTURE)
        corrupted[70] ^= 1
        with self.assertRaisesRegex(fw.FirmwarePatchError, "SHA-256"):
            fw.validate_original(bytes(corrupted), FIXTURE_SPEC)

    def test_rejects_missing_or_duplicate_version_string(self):
        missing = FIXTURE.replace(b"B077T_US_13", b"B077T_US_12")
        duplicate = FIXTURE + b"B077T_US_13"
        missing_spec = fw.FirmwareSpec(
            len(missing), hashlib.sha256(missing).hexdigest(), b"B077T_US_13",
            FIXTURE_PATCHES, 0,
        )
        duplicate_spec = fw.FirmwareSpec(
            len(duplicate), hashlib.sha256(duplicate).hexdigest(), b"B077T_US_13",
            FIXTURE_PATCHES, 0,
        )
        with self.assertRaisesRegex(fw.FirmwarePatchError, "exactly once"):
            fw.validate_original(missing, missing_spec)
        with self.assertRaisesRegex(fw.FirmwarePatchError, "exactly once"):
            fw.validate_original(duplicate, duplicate_spec)

    def test_rejects_wrong_byte_at_patch_offset_even_with_matching_hash(self):
        changed = bytearray(FIXTURE)
        changed[40] = 0x38
        changed = bytes(changed)
        spec = fw.FirmwareSpec(
            len(changed), hashlib.sha256(changed).hexdigest(), b"B077T_US_13",
            FIXTURE_PATCHES, 0,
        )
        with self.assertRaisesRegex(fw.FirmwarePatchError, r"0x28.*expected 0x39"):
            fw.validate_original(changed, spec)

    def test_approved_spec_delegates_to_common_internal_validation(self):
        approved = replace(
            images.APPROVED_IMAGES[images.ImageKind.GLOBAL],
            size=len(FIXTURE),
            sha256=FIXTURE_SHA256,
            full_file_sum16=0,
        )
        approved_images = MappingProxyType({images.ImageKind.GLOBAL: approved})
        spec = fw.FirmwareSpec(
            size=len(FIXTURE),
            sha256=FIXTURE_SHA256,
            version=b"B077T_US_13",
            patches=FIXTURE_PATCHES,
            patched_sum16=0x0CEA,
            approved_kind=images.ImageKind.GLOBAL,
        )

        with patch.object(images, "APPROVED_IMAGES", approved_images):
            with self.assertRaisesRegex(fw.FirmwarePatchError, "sum16"):
                fw.validate_original(FIXTURE, spec)


class PatchingTests(unittest.TestCase):
    def test_changes_only_the_six_declared_offsets(self):
        patched = fw.patch_firmware(FIXTURE, FIXTURE_SPEC)
        differences = tuple(
            (index, before, after)
            for index, (before, after) in enumerate(zip(FIXTURE, patched, strict=True))
            if before != after
        )
        self.assertEqual(
            differences,
            (
                (40, 0x39, 0xE0), (42, 0xE0, 0xE2),
                (44, 0xE2, 0xE3), (46, 0xE3, 0x91),
                (48, 0xE7, 0x90), (50, 0xE6, 0xE7),
            ),
        )
        self.assertEqual(len(patched), len(FIXTURE))
        self.assertEqual(sum(patched) & 0xFFFF, 0x0CEA)

    def test_patch_declaration_order_does_not_change_diff_validation(self):
        reordered = fw.FirmwareSpec(
            FIXTURE_SPEC.size,
            FIXTURE_SPEC.sha256,
            FIXTURE_SPEC.version,
            tuple(reversed(FIXTURE_PATCHES)),
            FIXTURE_SPEC.patched_sum16,
        )

        patched = fw.patch_firmware(FIXTURE, reordered)

        self.assertEqual(sum(patched) & 0xFFFF, 0x0CEA)

    def test_rejects_an_undeclared_difference_in_generated_output(self):
        patched = bytearray(fw.patch_firmware(FIXTURE, FIXTURE_SPEC))
        patched[70] ^= 1
        with self.assertRaisesRegex(fw.FirmwarePatchError, "unexpected differences"):
            fw.verify_patched(FIXTURE, bytes(patched), FIXTURE_SPEC)


class FileBuildTests(unittest.TestCase):
    def test_direct_script_help_preserves_cli_entrypoint(self):
        result = subprocess.run(
            [sys.executable, "tools/phase3_build_patch.py", "--help"],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Validate GLOBAL firmware", result.stdout)

    def test_writes_verified_copy_and_patched_firmware_without_altering_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download.bin"
            original = root / "original" / "B077T_US_13.bin"
            patched = root / "patched" / "B077T_US_13_JP_LANG.bin"
            source.write_bytes(FIXTURE)

            result = fw.build_files(source, original, patched, spec=FIXTURE_SPEC)

            self.assertEqual(source.read_bytes(), FIXTURE)
            self.assertEqual(original.read_bytes(), FIXTURE)
            self.assertEqual(patched.read_bytes(), fw.patch_firmware(FIXTURE, FIXTURE_SPEC))
            self.assertEqual(result.size, 96)
            self.assertEqual(result.original_sha256, FIXTURE_SHA256)
            self.assertEqual(result.patched_sum16, 0x0CEA)

    def test_rejects_existing_output_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download.bin"
            original = root / "original.bin"
            patched = root / "patched.bin"
            source.write_bytes(FIXTURE)
            original.write_bytes(b"keep")
            with self.assertRaisesRegex(fw.FirmwarePatchError, "already exists"):
                fw.build_files(source, original, patched, spec=FIXTURE_SPEC)
            self.assertEqual(original.read_bytes(), b"keep")
            self.assertFalse(patched.exists())

    def test_rejects_input_equal_to_either_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "same.bin"
            source.write_bytes(FIXTURE)
            with self.assertRaisesRegex(fw.FirmwarePatchError, "same path"):
                fw.build_files(source, source, Path(directory) / "patched.bin", spec=FIXTURE_SPEC)
            with self.assertRaisesRegex(fw.FirmwarePatchError, "same path"):
                fw.build_files(source, Path(directory) / "original.bin", source, spec=FIXTURE_SPEC)

    def test_cli_prints_checksums_and_six_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download.bin"
            source.write_bytes(FIXTURE)
            stdout = io.StringIO()
            with patch.object(fw, "GLOBAL_SPEC", FIXTURE_SPEC), redirect_stdout(stdout):
                code = fw.main([str(source), "--output-root", str(root)])
            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("original SHA-256: " + FIXTURE_SHA256, output)
            self.assertIn("patched sum16: 0x0cea", output)
            self.assertEqual(output.count("->"), 6)
            self.assertTrue((root / "firmware" / "original" / "B077T_US_13.bin").is_file())
            self.assertTrue(
                (root / "firmware" / "patched" / "B077T_US_13_JP_LANG.bin").is_file()
            )


if __name__ == "__main__":
    unittest.main()
