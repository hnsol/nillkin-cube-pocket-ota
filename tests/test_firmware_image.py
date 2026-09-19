import hashlib
import struct
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from tools import firmware_image as images

GLOBAL_PATH = Path("firmware/original/B077T_US_13.bin")
JP_LANG_PATH = Path("firmware/patched/B077T_US_13_JP_LANG.bin")
GLOBAL_SHA = "00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f"
JP_LANG_SHA = "3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246"
PATCHED_OFFSETS = (0x1DABE, 0x1DB3E, 0x1DB4A, 0x1DB4C, 0x1DB56, 0x1DB58)


def make_image(
    *,
    version: bytes = b"B077T_US_13",
    hardware_model: bytes = b"PAR2801",
    marker: bytes = b"MJNK",
    keymap: tuple[int, ...] = tuple(range(130)),
) -> bytes:
    return (
        b"HEAD-"
        + version
        + b"\x00"
        + hardware_model
        + b"\x00"
        + marker
        + struct.pack("<130H", *keymap)
        + b"-TAIL"
    )


def matching_profile(data: bytes, **changes) -> images.FirmwareProfile:
    marker_offset = data.index(b"MJNK")
    profile = images.FirmwareProfile(
        kind=images.ImageKind.GLOBAL,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        full_file_sum16=sum(data) & 0xFFFF,
        embedded_version=b"B077T_US_13",
        hardware_model=b"PAR2801",
        keymap_marker_offset=marker_offset,
    )
    return replace(profile, **changes)


class ApprovedProfileTests(unittest.TestCase):
    def test_approved_profiles_are_immutable_and_exact(self):
        global_profile = images.APPROVED_IMAGES[images.ImageKind.GLOBAL]
        jp_profile = images.APPROVED_IMAGES[images.ImageKind.JP_LANG]

        self.assertIsInstance(images.APPROVED_IMAGES, MappingProxyType)
        self.assertEqual(global_profile.sha256, GLOBAL_SHA)
        self.assertEqual(global_profile.size, 123_916)
        self.assertEqual(global_profile.full_file_sum16, 0xEC27)
        self.assertEqual(jp_profile.sha256, JP_LANG_SHA)
        self.assertEqual(jp_profile.size, 123_916)
        self.assertEqual(jp_profile.full_file_sum16, 0xEC29)
        for profile in (global_profile, jp_profile):
            self.assertEqual(profile.embedded_version, b"B077T_US_13")
            self.assertEqual(profile.hardware_model, b"PAR2801")
            self.assertEqual(profile.keymap_marker_offset, 0x1DAA0)
        with self.assertRaises(TypeError):
            images.APPROVED_IMAGES[images.ImageKind.GLOBAL] = jp_profile
        with self.assertRaises(FrozenInstanceError):
            global_profile.size = 0

    @unittest.skipUnless(GLOBAL_PATH.is_file(), "approved GLOBAL fixture unavailable")
    def test_rejects_unapproved_hash_even_when_size_and_version_match(self):
        corrupted = bytearray(GLOBAL_PATH.read_bytes())
        corrupted[100] ^= 1

        with self.assertRaisesRegex(images.ImageValidationError, "SHA-256"):
            images.validate_image(bytes(corrupted))

    @unittest.skipUnless(
        GLOBAL_PATH.is_file() and JP_LANG_PATH.is_file(),
        "approved firmware fixtures unavailable",
    )
    def test_validates_both_approved_images_and_parses_130_entries(self):
        global_image = images.validate_image(GLOBAL_PATH.read_bytes())
        jp_image = images.validate_image(JP_LANG_PATH.read_bytes())

        self.assertEqual(global_image.profile.kind, images.ImageKind.GLOBAL)
        self.assertEqual(jp_image.profile.kind, images.ImageKind.JP_LANG)
        self.assertEqual(len(global_image.keymap), 130)
        self.assertEqual(len(jp_image.keymap), 130)

    @unittest.skipUnless(
        GLOBAL_PATH.is_file() and JP_LANG_PATH.is_file(),
        "approved firmware fixtures unavailable",
    )
    def test_jp_lang_changes_exactly_six_little_endian_keymap_entries(self):
        global_data = GLOBAL_PATH.read_bytes()
        jp_data = JP_LANG_PATH.read_bytes()
        byte_differences = tuple(
            offset
            for offset, (before, after) in enumerate(
                zip(global_data, jp_data, strict=True)
            )
            if before != after
        )
        global_image = images.validate_image(global_data)
        jp_image = images.validate_image(jp_data)
        entry_differences = tuple(
            index
            for index, (before, after) in enumerate(
                zip(global_image.keymap, jp_image.keymap, strict=True)
            )
            if before != after
        )

        self.assertEqual(byte_differences, PATCHED_OFFSETS)
        self.assertEqual(entry_differences, (13, 77, 83, 84, 89, 90))
        for offset in PATCHED_OFFSETS:
            self.assertEqual(global_data[offset + 1], 0)
            self.assertEqual(jp_data[offset + 1], 0)


class StrictValidationTests(unittest.TestCase):
    def validate_with_profile(self, data: bytes, profile: images.FirmwareProfile):
        approved = MappingProxyType({profile.kind: profile})
        with patch.object(images, "APPROVED_IMAGES", approved):
            return images.validate_image(data)

    def test_revalidates_size_after_hash_selects_profile(self):
        data = make_image()
        profile = matching_profile(data, size=len(data) + 1)

        with self.assertRaisesRegex(images.ImageValidationError, "size"):
            self.validate_with_profile(data, profile)

    def test_revalidates_sum16_after_hash_selects_profile(self):
        data = make_image()
        profile = matching_profile(data, full_file_sum16=(sum(data) + 1) & 0xFFFF)

        with self.assertRaisesRegex(images.ImageValidationError, "sum16"):
            self.validate_with_profile(data, profile)

    def test_rejects_missing_or_duplicate_embedded_version(self):
        missing = make_image(version=b"B077T_US_12")
        duplicate = make_image() + b"B077T_US_13"

        with self.assertRaisesRegex(images.ImageValidationError, "version"):
            self.validate_with_profile(missing, matching_profile(missing))
        with self.assertRaisesRegex(images.ImageValidationError, "version"):
            self.validate_with_profile(duplicate, matching_profile(duplicate))

    def test_rejects_missing_or_conflicting_hardware_model(self):
        missing = make_image(hardware_model=b"PAR2802")
        conflicting = make_image() + b"PAR2802"

        with self.assertRaisesRegex(images.ImageValidationError, "hardware model"):
            self.validate_with_profile(missing, matching_profile(missing))
        with self.assertRaisesRegex(images.ImageValidationError, "hardware model"):
            self.validate_with_profile(conflicting, matching_profile(conflicting))

    def test_rejects_marker_at_wrong_offset_or_duplicate_marker(self):
        data = make_image()
        wrong_offset_profile = matching_profile(data, keymap_marker_offset=0)
        duplicate = data + b"MJNK" + bytes(260)

        with self.assertRaisesRegex(images.ImageValidationError, "MJNK offset"):
            self.validate_with_profile(data, wrong_offset_profile)
        with self.assertRaisesRegex(images.ImageValidationError, "multiple MJNK"):
            self.validate_with_profile(duplicate, matching_profile(duplicate))

    def test_rejects_truncated_little_endian_keymap(self):
        data = make_image(marker=b"MJNK")[:-10]

        with self.assertRaisesRegex(images.ImageValidationError, "keymap.*truncated"):
            self.validate_with_profile(data, matching_profile(data))


if __name__ == "__main__":
    unittest.main()
