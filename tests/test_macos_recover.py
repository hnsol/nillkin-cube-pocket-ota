import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools import firmware_image, gatt_ota, macos_ota, macos_recover


def global_image() -> firmware_image.ValidatedImage:
    return firmware_image.ValidatedImage(
        profile=firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.GLOBAL],
        keymap=tuple(range(130)),
    )


def jp_image() -> firmware_image.ValidatedImage:
    return firmware_image.ValidatedImage(
        profile=firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.JP_LANG],
        keymap=tuple(range(130)),
    )


class RecoveryCliTests(unittest.TestCase):
    def test_parser_requires_execute_and_sha_confirmation(self):
        args = macos_recover.build_parser().parse_args(
            [
                "--firmware",
                "global.bin",
                "--execute",
                "--confirm-sha256",
                global_image().profile.sha256,
            ]
        )

        self.assertTrue(args.execute)
        self.assertEqual(args.confirm_sha256, global_image().profile.sha256)

    def test_parser_accepts_factory_signature_override(self):
        args = macos_recover.build_parser().parse_args(
            [
                "--firmware",
                "global.bin",
                "--execute",
                "--confirm-sha256",
                global_image().profile.sha256,
                "--accept-factory-signature",
            ]
        )

        self.assertTrue(args.accept_factory_signature)


class RecoveryExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_authorizes_only_global_after_same_session_probe(self):
        report = macos_ota.PreflightReport(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            gatt_revision="1.0.0",
            vendor_ota_model="B077T_US_13",
            current_ota_version="1.0",
            current_ota_checksum=0x6162,
            target_image_kind="global",
            target_full_file_sum16=0xEC27,
            checksums_comparable=False,
            ready_for_future_flash=True,
            blockers=(),
        )
        engine = SimpleNamespace(recover=AsyncMock())
        with (
            patch.object(
                macos_ota,
                "collect_preflight_on_client",
                AsyncMock(return_value=report),
            ),
            patch.object(gatt_ota, "authorize_firmware", return_value=object()) as auth,
            patch.object(gatt_ota, "GattOtaEngine", return_value=engine),
        ):
            await macos_recover.recover_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=global_image(),
                data=b"global",
                operation_timeout=5,
            )

        auth.assert_called_once_with(b"global", "B077T_US_13", recovery=True)
        engine.recover.assert_awaited_once()

    async def test_recovery_accepts_matched_factory_signature_for_global(self):
        report = macos_ota.PreflightReport(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            gatt_revision="1.0.0",
            vendor_ota_model=None,
            current_ota_version="1.0",
            current_ota_checksum=0x6162,
            target_image_kind="global",
            target_full_file_sum16=0xEC27,
            checksums_comparable=False,
            ready_for_future_flash=True,
            blockers=(),
            factory_signature_matched=True,
        )
        engine = SimpleNamespace(recover=AsyncMock())
        with (
            patch.object(
                macos_ota,
                "collect_preflight_on_client",
                AsyncMock(return_value=report),
            ) as preflight,
            patch.object(gatt_ota, "authorize_firmware", return_value=object()) as auth,
            patch.object(gatt_ota, "GattOtaEngine", return_value=engine),
        ):
            await macos_recover.recover_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=global_image(),
                data=b"global",
                operation_timeout=5,
                accept_factory_signature=True,
            )

        preflight.assert_awaited_once()
        self.assertTrue(preflight.await_args.kwargs["accept_factory_signature"])
        auth.assert_called_once_with(b"global", "B077T_US_13", recovery=True)
        engine.recover.assert_awaited_once()

    async def test_recovery_refuses_factory_fallback_without_override(self):
        report = macos_ota.PreflightReport(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            gatt_revision="1.0.0",
            vendor_ota_model=None,
            current_ota_version="1.0",
            current_ota_checksum=0x6162,
            target_image_kind="global",
            target_full_file_sum16=0xEC27,
            checksums_comparable=False,
            ready_for_future_flash=True,
            blockers=(),
            factory_signature_matched=True,
        )
        with (
            patch.object(
                macos_ota,
                "collect_preflight_on_client",
                AsyncMock(return_value=report),
            ),
            patch.object(gatt_ota, "authorize_firmware") as auth,
            self.assertRaisesRegex(
                macos_recover.RecoveryPreflightError,
                "Vendor OTA model B077T",
            ),
        ):
            await macos_recover.recover_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=global_image(),
                data=b"global",
                operation_timeout=5,
            )

        auth.assert_not_called()

    async def test_recovery_refuses_jp_image_before_ble_engine(self):
        with (
            patch.object(gatt_ota, "GattOtaEngine") as engine,
            self.assertRaises(macos_recover.RecoveryPreflightError),
        ):
            await macos_recover.recover_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=jp_image(),
                data=b"jp",
                operation_timeout=5,
            )

        engine.assert_not_called()
