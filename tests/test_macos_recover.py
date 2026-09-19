import contextlib
import io
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools import firmware_image, gatt_ota, macos_ota, macos_recover, pixart_ota


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

    def test_parser_rejects_inspect_state_with_execute(self):
        with self.assertRaises(SystemExit):
            macos_recover.build_parser().parse_args(
                [
                    "--firmware",
                    "global.bin",
                    "--inspect-state",
                    "--execute",
                ]
            )

    def test_inspect_state_does_not_require_execute_or_confirmation(self):
        state = pixart_ota.OtaState(
            status=0,
            new_flow=1,
            offset=0,
            checksum=0,
            max_object_size=4096,
            mtu_size=244,
            prn_threshold=16,
            spec_result=1,
        )
        report = macos_recover.RecoveryStateReport(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            state=state,
            prefix_matches=True,
        )
        output = io.StringIO()
        with (
            patch("pathlib.Path.read_bytes", return_value=b"global"),
            patch.object(
                firmware_image, "validate_image", return_value=global_image()
            ),
            patch.object(
                macos_recover, "_run_inspect", AsyncMock(return_value=report)
            ) as run_inspect,
            contextlib.redirect_stdout(output),
        ):
            status = macos_recover.main(
                ["--firmware", "global.bin", "--inspect-state"]
            )

        self.assertEqual(status, 0)
        run_inspect.assert_awaited_once()
        rendered = output.getvalue()
        self.assertIn("Offset (objects): 0", rendered)
        self.assertIn("Checksum: 0x0000", rendered)
        self.assertIn("GLOBAL prefix match: yes", rendered)

    def test_inspect_state_prints_all_fields_and_mismatch(self):
        state = pixart_ota.OtaState(
            status=0,
            new_flow=1,
            offset=31,
            checksum=0xEC27,
            max_object_size=4096,
            mtu_size=244,
            prn_threshold=16,
            spec_result=1,
        )
        report = macos_recover.RecoveryStateReport(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            state=state,
            prefix_matches=False,
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            macos_recover.print_state_report(report)

        rendered = output.getvalue()
        for expected in (
            "Offset (objects): 31",
            "Checksum: 0xEC27",
            "Max object size: 4096",
            "MTU size: 244",
            "PRN threshold: 16",
            "GLOBAL prefix match: no",
            "FW書込み・確定・再起動は未実施",
        ):
            self.assertIn(expected, rendered)


class RecoveryExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_state_validates_identity_and_only_sends_0x27(self):
        class Characteristic:
            def __init__(self, uuid, properties):
                self.uuid = uuid
                self.properties = properties

        class Service:
            def __init__(self, uuid, characteristics):
                self.uuid = uuid
                self.characteristics = characteristics

        ff01 = Characteristic(
            "ff01", ["read", "write", "write-without-response", "notify"]
        )
        model = Characteristic("2a24", ["read"])
        services = [
            Service(
                "ff00",
                [
                    ff01,
                    Characteristic("ff02", ["read", "write"]),
                    Characteristic("ff03", ["read", "write"]),
                ],
            ),
            Service("180a", [model]),
        ]
        state_raw = b"\x0e\x10\x27\x00\x01" + struct.pack(
            "<HHIHHB", 1, 10, 4, 244, 16, 1
        )

        class Client:
            is_connected = True

            def __init__(self):
                self.services = services
                self.events = []

            async def read_gatt_char(self, characteristic):
                self.events.append(("read", characteristic.uuid))
                if characteristic is model:
                    return b"PAR2801\x00"
                return state_raw

            async def write_gatt_char(self, characteristic, payload, *, response):
                self.events.append(
                    ("write", characteristic.uuid, bytes(payload), response)
                )

        client = Client()
        data = bytes(range(1, 7))

        report = await macos_recover.inspect_state_on_client(
            client,
            advertised_name="Cube Pocket Keyboard 3",
            image=global_image(),
            data=data,
            operation_timeout=0.1,
            settle_seconds=0,
        )

        self.assertTrue(report.prefix_matches)
        writes = [event for event in client.events if event[0] == "write"]
        self.assertEqual(
            writes,
            [("write", "ff01", b"\x27\x06\x00\x00\x00\x00", True)],
        )

    async def test_inspect_state_rejects_wrong_gatt_model_before_0x27(self):
        client = SimpleNamespace(services=[])
        with (
            patch.object(
                macos_recover.phase1_ble_info,
                "inspect_gatt",
                return_value={
                    "ff01": SimpleNamespace(characteristic="ff01"),
                    "ff02": SimpleNamespace(characteristic="ff02"),
                    "ff03": SimpleNamespace(characteristic="ff03"),
                },
            ),
            patch.object(
                macos_recover.phase1_ble_info,
                "_read_optional_device_info",
                AsyncMock(return_value="OTHER"),
            ),
            patch.object(gatt_ota, "GattOtaEngine") as engine,
            self.assertRaisesRegex(
                macos_recover.RecoveryPreflightError, "PAR2801"
            ),
        ):
            await macos_recover.inspect_state_on_client(
                client,
                advertised_name="Cube Pocket Keyboard 3",
                image=global_image(),
                data=b"global",
                operation_timeout=0.1,
            )

        engine.assert_not_called()

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
