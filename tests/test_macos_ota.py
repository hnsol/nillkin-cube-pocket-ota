import argparse
import asyncio
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.fakes import GattStep, ScriptedGattClient
from tools import (
    ble_transport,
    firmware_image,
    gatt_ota,
    keymap_config,
    ota_protocol,
    phase1_ble_info,
)
from tools import macos_ota as ota

FW_INFO_FRAME = bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61")
INSTALLED_GLOBAL_FW_INFO_FRAME = bytes.fromhex(
    "0e 09 23 00 31 2e 30 2e 31 27 ec"
)
MODEL_INFO_FRAME = bytes.fromhex(
    "0e 17 2b 00 00 00 42 30 37 37 54 5f 55 53 5f 31 33 00 00 00 00 00 00 00 00"
)
EXPECTED_SERVICES = ("ff00", "ff01", "ff02", "ff03")


def approved_image() -> firmware_image.ValidatedImage:
    return firmware_image.ValidatedImage(
        profile=firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.GLOBAL],
        keymap=tuple(range(130)),
    )


def approved_jp_image() -> firmware_image.ValidatedImage:
    return firmware_image.ValidatedImage(
        profile=firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.JP_LANG],
        keymap=tuple(range(130)),
    )


def expected_identity(**changes) -> ble_transport.GattIdentity:
    values = {
        "advertised_name": phase1_ble_info.TARGET_NAME,
        "gatt_model": "PAR2801",
        "gatt_revision": "1.0.0",
        "service_uuids": EXPECTED_SERVICES,
    }
    values.update(changes)
    return ble_transport.build_identity(**values)


def current_firmware() -> ota_protocol.OtaFirmwareInfo:
    return ota_protocol.parse_firmware_info(FW_INFO_FRAME)


def installed_global_firmware() -> ota_protocol.OtaFirmwareInfo:
    return ota_protocol.parse_firmware_info(INSTALLED_GLOBAL_FW_INFO_FRAME)


def normal_script() -> list[GattStep]:
    return [
        GattStep("read", b"PAR2801\x00"),
        GattStep("read", b"1.0.0\x00"),
        GattStep("read", b"BOOT"),
        GattStep("write", b"\x10\x00", response=True),
        GattStep("read", bytes.fromhex("0e 02 10 00")),
        GattStep("write", b"\x23\x00", response=True),
        GattStep("read", FW_INFO_FRAME),
    ]


class PreflightEvaluationTests(unittest.TestCase):
    def test_each_exact_allowlisted_name_can_satisfy_name_gate(self):
        for name in phase1_ble_info.TARGET_NAMES:
            with self.subTest(name=name):
                report = ota.evaluate_preflight(
                    expected_identity(advertised_name=name),
                    "B077T",
                    current_firmware(),
                    approved_image(),
                )

                self.assertTrue(report.ready_for_future_flash)

    def test_preflight_is_not_flashable_without_b077t_model(self):
        report = ota.evaluate_preflight(
            expected_identity(), None, current_firmware(), approved_image()
        )

        self.assertFalse(report.ready_for_future_flash)
        self.assertIn("B077T", report.blockers[0])

    def test_preflight_separates_device_checksum_from_file_sum16(self):
        report = ota.evaluate_preflight(
            expected_identity(), "B077T", current_firmware(), approved_image()
        )

        self.assertEqual(report.current_ota_checksum, 0x6162)
        self.assertEqual(report.target_full_file_sum16, 0xEC27)
        self.assertFalse(report.checksums_comparable)

    def test_every_identity_and_image_gate_must_pass(self):
        cases = (
            (expected_identity(advertised_name="other"), "B077T", approved_image()),
            (expected_identity(gatt_model="PAR9999"), "B077T", approved_image()),
            (
                expected_identity(service_uuids=("ff00", "ff01", "ff02")),
                "B077T",
                approved_image(),
            ),
            (expected_identity(), "OTHER", approved_image()),
            (expected_identity(), "B077T", None),
        )

        for identity, model, image in cases:
            with self.subTest(identity=identity, model=model, image=image):
                report = ota.evaluate_preflight(
                    identity, model, current_firmware(), image
                )
                self.assertFalse(report.ready_for_future_flash)
                self.assertTrue(report.blockers)

    def test_ready_wording_does_not_claim_transfer_is_available(self):
        report = ota.evaluate_preflight(
            expected_identity(), "B077T", current_firmware(), approved_image()
        )
        output = io.StringIO()

        with redirect_stdout(output):
            ota.print_report(report)

        rendered = output.getvalue()
        self.assertTrue(report.ready_for_future_flash)
        self.assertIn("将来のwrite preflight gateを満たす", rendered)
        self.assertNotIn("転送可能", rendered)

    def test_exact_factory_signature_allows_only_approved_global(self):
        report = ota.evaluate_preflight(
            expected_identity(),
            ota_protocol.ModelIdentity.UNAVAILABLE,
            current_firmware(),
            approved_image(),
            accept_factory_signature=True,
        )

        self.assertTrue(report.ready_for_future_flash)
        self.assertIsNone(report.vendor_ota_model)
        self.assertTrue(report.factory_signature_matched)

        output = io.StringIO()
        with redirect_stdout(output):
            ota.print_report(report)
        self.assertIn("Factory signature: matched", output.getvalue())
        self.assertIn("Vendor OTA model: unavailable", output.getvalue())

    def test_factory_signature_requires_every_fingerprint_component(self):
        cases = (
            expected_identity(advertised_name="Cube Pocket Keyboard 2"),
            expected_identity(gatt_model="PAR9999"),
            expected_identity(gatt_revision="1.0.1"),
            expected_identity(service_uuids=("ff00", "ff01", "ff02")),
        )
        for identity in cases:
            with self.subTest(identity=identity):
                report = ota.evaluate_preflight(
                    identity,
                    ota_protocol.ModelIdentity.UNAVAILABLE,
                    current_firmware(),
                    approved_image(),
                    accept_factory_signature=True,
                )
                self.assertFalse(report.factory_signature_matched)
                self.assertFalse(report.ready_for_future_flash)

        wrong_raw = ota_protocol.OtaFirmwareInfo(
            version="1.0", checksum=0x6162, raw=FW_INFO_FRAME[:-1] + b"\x60"
        )
        report = ota.evaluate_preflight(
            expected_identity(),
            ota_protocol.ModelIdentity.UNAVAILABLE,
            wrong_raw,
            approved_image(),
            accept_factory_signature=True,
        )
        self.assertFalse(report.factory_signature_matched)
        self.assertFalse(report.ready_for_future_flash)

    def test_factory_signature_rejects_jp_lang_and_configured_targets(self):
        for kind in (
            firmware_image.ImageKind.JP_LANG,
            firmware_image.ImageKind.CONFIGURED,
        ):
            profile = firmware_image.FirmwareProfile(
                kind=kind,
                size=1,
                sha256="not-global",
                full_file_sum16=0,
                embedded_version=b"B077T_US_13",
                hardware_model=b"PAR2801",
                keymap_marker_offset=0,
            )
            with self.subTest(kind=kind):
                report = ota.evaluate_preflight(
                    expected_identity(),
                    ota_protocol.ModelIdentity.UNAVAILABLE,
                    current_firmware(),
                    firmware_image.ValidatedImage(profile=profile, keymap=()),
                    accept_factory_signature=True,
                )
                self.assertFalse(report.ready_for_future_flash)
                self.assertFalse(report.factory_signature_matched)

    def test_installed_global_signature_accepts_every_allowlisted_slot_for_jp_lang(self):
        for name in phase1_ble_info.TARGET_NAMES:
            with self.subTest(name=name):
                report = ota.evaluate_preflight(
                    expected_identity(advertised_name=name),
                    ota_protocol.ModelIdentity.UNAVAILABLE,
                    installed_global_firmware(),
                    approved_jp_image(),
                    accept_installed_global_signature=True,
                )

                self.assertTrue(report.ready_for_future_flash)
                self.assertTrue(report.installed_global_signature_matched)

        output = io.StringIO()
        with redirect_stdout(output):
            ota.print_report(report)
        self.assertIn("Installed GLOBAL signature: matched", output.getvalue())

    def test_installed_global_signature_requires_every_fingerprint_component(self):
        cases = (
            expected_identity(advertised_name="other"),
            expected_identity(gatt_model="PAR9999"),
            expected_identity(gatt_revision="1.0.1"),
            expected_identity(service_uuids=("ff00", "ff01", "ff02")),
        )
        for identity in cases:
            with self.subTest(identity=identity):
                report = ota.evaluate_preflight(
                    identity,
                    ota_protocol.ModelIdentity.UNAVAILABLE,
                    installed_global_firmware(),
                    approved_jp_image(),
                    accept_installed_global_signature=True,
                )
                self.assertFalse(report.installed_global_signature_matched)
                self.assertFalse(report.ready_for_future_flash)

        report = ota.evaluate_preflight(
            expected_identity(),
            ota_protocol.ModelIdentity.UNAVAILABLE,
            ota_protocol.OtaFirmwareInfo(
                version="1.0.1",
                checksum=0xEC27,
                raw=INSTALLED_GLOBAL_FW_INFO_FRAME[:-1] + b"\x26",
            ),
            approved_jp_image(),
            accept_installed_global_signature=True,
        )
        self.assertFalse(report.installed_global_signature_matched)
        self.assertFalse(report.ready_for_future_flash)

    def test_installed_global_signature_rejects_global_target(self):
        report = ota.evaluate_preflight(
            expected_identity(),
            ota_protocol.ModelIdentity.UNAVAILABLE,
            installed_global_firmware(),
            approved_image(),
            accept_installed_global_signature=True,
        )

        self.assertFalse(report.installed_global_signature_matched)
        self.assertFalse(report.ready_for_future_flash)


class ParserSafetyTests(unittest.TestCase):
    def test_cli_accepts_factory_signature_and_vendor_probe_flags(self):
        factory = ota.build_parser().parse_args(
            ["--firmware", "fw.bin", "--accept-factory-signature"]
        )
        probe = ota.build_parser().parse_args(
            ["--firmware", "fw.bin", "--probe-vendor-model"]
        )

        self.assertTrue(factory.accept_factory_signature)
        self.assertTrue(probe.probe_vendor_model)

        installed = ota.build_parser().parse_args(
            ["--firmware", "fw.bin", "--accept-installed-global-signature"]
        )
        self.assertTrue(installed.accept_installed_global_signature)

    def test_cli_rejects_installed_global_signature_without_execute_or_with_other_gates(
        self,
    ):
        cases = (
            ["--firmware", "unused.bin", "--accept-installed-global-signature"],
            [
                "--firmware",
                "unused.bin",
                "--execute",
                "--accept-installed-global-signature",
                "--probe-vendor-model",
                "--confirm-sha256",
                "unused",
            ],
            [
                "--firmware",
                "unused.bin",
                "--execute",
                "--accept-installed-global-signature",
                "--accept-factory-signature",
                "--confirm-sha256",
                "unused",
            ],
        )
        for args in cases:
            with self.subTest(args=args):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    status = ota.main(args)
                self.assertEqual(status, 2)

    def test_cli_rejects_factory_signature_without_execute_and_flag_combination(self):
        for args in (
            ["--firmware", "unused.bin", "--accept-factory-signature"],
            [
                "--firmware",
                "unused.bin",
                "--execute",
                "--accept-factory-signature",
                "--probe-vendor-model",
                "--confirm-sha256",
                "unused",
            ],
        ):
            with self.subTest(args=args):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    status = ota.main(args)
                self.assertEqual(status, 2)

    def test_cli_execute_requires_exact_target_sha256(self):
        args = ota.build_parser().parse_args(
            [
                "--firmware",
                "fw.bin",
                "--execute",
                "--confirm-sha256",
                approved_image().profile.sha256,
            ]
        )

        self.assertTrue(args.execute)
        self.assertEqual(args.confirm_sha256, approved_image().profile.sha256)

    def test_cli_execute_without_confirmation_fails_before_ble(self):
        output = io.StringIO()
        with redirect_stderr(output):
            status = ota.main(["--firmware", "unused.bin", "--execute"])

        self.assertEqual(status, 2)
        self.assertIn("confirm-sha256", output.getvalue())

    def test_execute_interrupt_warns_that_ota_may_be_incomplete(self):
        stderr = io.StringIO()
        image = approved_image()

        def interrupt(coroutine):
            coroutine.close()
            raise KeyboardInterrupt

        with (
            patch.object(Path, "read_bytes", return_value=b"approved"),
            patch.object(firmware_image, "validate_image", return_value=image),
            patch.object(asyncio, "run", side_effect=interrupt),
            redirect_stderr(stderr),
        ):
            status = ota.main(
                [
                    "--firmware",
                    "fw.bin",
                    "--execute",
                    "--confirm-sha256",
                    image.profile.sha256,
                ]
            )

        self.assertEqual(status, 130)
        self.assertIn("OTAが未完了", stderr.getvalue())
        self.assertNotIn("FWデータは送信していません", stderr.getvalue())

    def test_cli_rejects_unpaired_configured_firmware_inputs_before_file_access(self):
        for args in (
            ["--firmware", "target.bin", "--base-firmware", "base.bin"],
            ["--firmware", "target.bin", "--remap-config", "layout.toml"],
        ):
            with self.subTest(args=args):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    status = ota.main(args)
                self.assertEqual(status, 2)
                self.assertIn("base-firmware", stderr.getvalue())

    def test_cli_accepts_show_transfer_plan(self):
        args = ota.build_parser().parse_args(
            ["--firmware", "fw.bin", "--show-transfer-plan"]
        )

        self.assertTrue(args.show_transfer_plan)

    def test_show_transfer_plan_validates_file_without_starting_ble(self):
        output = io.StringIO()
        data = b"\x00" * approved_image().profile.size
        with (
            patch.object(Path, "read_bytes", return_value=data),
            patch.object(
                firmware_image, "validate_image", return_value=approved_image()
            ) as validate,
            patch.object(
                ota,
                "_run",
                side_effect=AssertionError("BLE path must not run"),
            ) as run,
            redirect_stdout(output),
        ):
            status = ota.main(["--firmware", "fw.bin", "--show-transfer-plan"])

        self.assertEqual(status, 0)
        validate.assert_called_once_with(data)
        run.assert_not_called()
        rendered = output.getvalue()
        self.assertIn(
            "Target SHA-256: "
            "00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f",
            rendered,
        )
        self.assertIn("Target size: 123916 bytes", rendered)
        self.assertIn("Target full-file sum16: 0xEC27", rendered)
        self.assertIn(
            "0x27 init-new send: 27 0c e4 01 00 00 (host→device; with response)",
            rendered,
        )
        self.assertIn("0x27 state response: ff01 read (device→host)", rendered)
        self.assertIn(
            "0x25 object-create send: 実機0x27応答で決定 (host→device; with response)",
            rendered,
        )
        self.assertIn("0x25 object ACK: notify待ち (device→host)", rendered)
        self.assertIn(
            "raw payload send: 実機0x27応答で決定 (host→device; without response)",
            rendered,
        )
        self.assertIn("0x17 PRN ACK: notify待ち (device→host)", rendered)
        self.assertIn(
            "0x18 upgrade send: version[5]=1.0.1 (host→device; with response)",
            rendered,
        )
        self.assertIn("0x18 upgrade ACK: notify待ち (device→host)", rendered)
        self.assertIn(
            "0x22 reset send: 22 00 (host→device; without response)", rendered
        )
        self.assertIn("Executable: --executeとSHA-256確認時のみ", rendered)
        self.assertIn("retransmit: ff02へ0x28", rendered)

    @unittest.skipUnless(
        Path("firmware/original/B077T_US_13.bin").is_file()
        and Path("firmware/patched/B077T_US_13_JP_LANG.bin").is_file(),
        "approved firmware fixtures unavailable",
    )
    def test_show_transfer_plan_accepts_only_the_exact_configured_regeneration(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = ota.main(
                [
                    "--firmware",
                    "firmware/patched/B077T_US_13_JP_LANG.bin",
                    "--base-firmware",
                    "firmware/original/B077T_US_13.bin",
                    "--remap-config",
                    "configs/jp-lang.toml",
                    "--show-transfer-plan",
                ]
            )

        self.assertEqual(status, 0)
        self.assertIn("Target image: configured", output.getvalue())

    def test_show_transfer_plan_module_import_does_not_import_bleak(self):
        script = """
import builtins

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'bleak':
        raise RuntimeError('Bleak import is forbidden for transfer-plan mode')
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import tools.macos_ota
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_cli_has_no_execute_or_state_changing_options(self):
        parser = ota.build_parser()

        for option in ("--erase", "--reset", "--chunk-size", "--resume"):
            with (
                self.subTest(option=option),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(["--firmware", "fw.bin", option])

        help_text = parser.format_help().lower()
        for term in ("erase", "reset", "chunk", "resume"):
            self.assertNotIn(term, help_text)


class ExecuteSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_rejects_installed_signature_with_other_authorization_gate(self):
        for options in (
            {"probe_vendor_model": True},
            {"accept_factory_signature": True},
        ):
            with (
                self.subTest(options=options),
                patch.object(
                    ota, "collect_preflight_on_client", AsyncMock()
                ) as collect,
                self.assertRaisesRegex(ota.ExecutePreflightError, "併用"),
            ):
                await ota.execute_on_client(
                    object(),
                    advertised_name="Cube Pocket Keyboard 1",
                    image=approved_jp_image(),
                    data=b"approved-data",
                    operation_timeout=5,
                    accept_installed_global_signature=True,
                    **options,
                )

            collect.assert_not_awaited()

    async def test_installed_global_fallback_authorizes_jp_with_global_model(self):
        report = ota.PreflightReport(
            advertised_name="Cube Pocket Keyboard 1",
            gatt_model="PAR2801",
            gatt_revision="1.0.0",
            vendor_ota_model=None,
            current_ota_version="1.0.1",
            current_ota_checksum=0xEC27,
            target_image_kind="jp_lang",
            target_full_file_sum16=0xEC29,
            checksums_comparable=False,
            ready_for_future_flash=True,
            blockers=(),
            installed_global_signature_matched=True,
        )
        engine = SimpleNamespace(flash=AsyncMock())
        data = b"approved-data"
        with (
            patch.object(
                ota, "collect_preflight_on_client", AsyncMock(return_value=report)
            ),
            patch.object(
                gatt_ota, "authorize_firmware", return_value=object()
            ) as authorize,
            patch.object(gatt_ota, "GattOtaEngine", return_value=engine),
        ):
            await ota.execute_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 1",
                image=approved_jp_image(),
                data=data,
                operation_timeout=5,
                accept_installed_global_signature=True,
            )

        authorize.assert_called_once_with(data, "B077T_US_13", recovery=False)
        engine.flash.assert_awaited_once()

    async def test_execute_rejects_factory_and_vendor_probe_combination(self):
        with (
            patch.object(ota, "collect_preflight_on_client", AsyncMock()) as collect,
            self.assertRaisesRegex(ota.ExecutePreflightError, "併用"),
        ):
            await ota.execute_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=approved_image(),
                data=b"approved-data",
                operation_timeout=5,
                probe_vendor_model=True,
                accept_factory_signature=True,
            )

        collect.assert_not_awaited()

    async def test_factory_fallback_authorizes_global_with_embedded_model(self):
        report = ota.PreflightReport(
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
        engine = SimpleNamespace(flash=AsyncMock())
        data = b"approved-data"
        with (
            patch.object(
                ota, "collect_preflight_on_client", AsyncMock(return_value=report)
            ),
            patch.object(
                gatt_ota, "authorize_firmware", return_value=object()
            ) as authorize,
            patch.object(gatt_ota, "GattOtaEngine", return_value=engine),
        ):
            await ota.execute_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=approved_image(),
                data=data,
                operation_timeout=5,
                accept_factory_signature=True,
            )

        authorize.assert_called_once_with(data, "B077T_US_13", recovery=False)
        engine.flash.assert_awaited_once()

    async def test_configured_flash_requires_successful_explicit_vendor_probe(self):
        report = ota.PreflightReport(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            gatt_revision="1.0.0",
            vendor_ota_model="B077T_US_13",
            current_ota_version="1.0",
            current_ota_checksum=0x6162,
            target_image_kind="configured",
            target_full_file_sum16=0xEC28,
            checksums_comparable=False,
            ready_for_future_flash=True,
            blockers=(),
        )
        config = keymap_config.KeymapConfig({"caps_lock": 0xE0})
        image = firmware_image.ValidatedImage(
            profile=firmware_image.FirmwareProfile(
                kind=firmware_image.ImageKind.CONFIGURED,
                size=1,
                sha256="configured",
                full_file_sum16=0,
                embedded_version=b"B077T_US_13",
                hardware_model=b"PAR2801",
                keymap_marker_offset=0,
            ),
            keymap=(),
        )
        with (
            patch.object(
                ota, "collect_preflight_on_client", AsyncMock(return_value=report)
            ),
            self.assertRaisesRegex(ota.ExecutePreflightError, "probe-vendor-model"),
        ):
            await ota.execute_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=image,
                data=b"target",
                base_data=b"base",
                config=config,
                operation_timeout=5,
                probe_vendor_model=False,
            )

    async def test_execute_uses_preflight_vendor_model_to_authorize_same_client(self):
        client = object()
        report = ota.PreflightReport(
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
        engine = SimpleNamespace(flash=AsyncMock())
        data = b"approved-data"

        with (
            patch.object(
                ota, "collect_preflight_on_client", AsyncMock(return_value=report)
            ) as preflight,
            patch.object(
                gatt_ota, "authorize_firmware", return_value=object()
            ) as authorize,
            patch.object(
                gatt_ota, "GattOtaEngine", return_value=engine
            ) as engine_class,
        ):
            await ota.execute_on_client(
                client,
                advertised_name="Cube Pocket Keyboard 3",
                image=approved_image(),
                data=data,
                operation_timeout=5,
            )

        self.assertIs(preflight.await_args.args[0], client)
        authorize.assert_called_once_with(data, "B077T_US_13", recovery=False)
        engine_class.assert_called_once_with(client, operation_timeout=5)
        engine.flash.assert_awaited_once()

    async def test_execute_refuses_when_same_session_preflight_has_blocker(self):
        report = ota.PreflightReport(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            gatt_revision="1.0.0",
            vendor_ota_model=None,
            current_ota_version="1.0",
            current_ota_checksum=0x6162,
            target_image_kind="global",
            target_full_file_sum16=0xEC27,
            checksums_comparable=False,
            ready_for_future_flash=False,
            blockers=("Vendor OTA model B077Tを確認できません",),
        )

        with (
            patch.object(
                ota, "collect_preflight_on_client", AsyncMock(return_value=report)
            ),
            patch.object(gatt_ota, "GattOtaEngine") as engine_class,
            self.assertRaises(ota.ExecutePreflightError),
        ):
            await ota.execute_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=approved_image(),
                data=b"approved-data",
                operation_timeout=5,
            )

        engine_class.assert_not_called()

    async def test_execute_uses_configured_authorization_only_with_base_and_config(
        self,
    ):
        report = ota.PreflightReport(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            gatt_revision="1.0.0",
            vendor_ota_model="B077T_US_13",
            current_ota_version="1.0",
            current_ota_checksum=0x6162,
            target_image_kind="configured",
            target_full_file_sum16=0xEC28,
            checksums_comparable=False,
            ready_for_future_flash=True,
            blockers=(),
        )
        engine = SimpleNamespace(flash=AsyncMock())
        config = keymap_config.KeymapConfig({"caps_lock": 0xE0})
        with (
            patch.object(
                ota, "collect_preflight_on_client", AsyncMock(return_value=report)
            ),
            patch.object(gatt_ota, "authorize_firmware") as fixed_auth,
            patch.object(
                gatt_ota, "authorize_configured_firmware", return_value=object()
            ) as configured_auth,
            patch.object(gatt_ota, "GattOtaEngine", return_value=engine),
        ):
            await ota.execute_on_client(
                object(),
                advertised_name="Cube Pocket Keyboard 3",
                image=firmware_image.ValidatedImage(
                    profile=firmware_image.FirmwareProfile(
                        kind=firmware_image.ImageKind.CONFIGURED,
                        size=1,
                        sha256="configured",
                        full_file_sum16=0,
                        embedded_version=b"B077T_US_13",
                        hardware_model=b"PAR2801",
                        keymap_marker_offset=0,
                    ),
                    keymap=(),
                ),
                data=b"target",
                base_data=b"base",
                config=config,
                operation_timeout=5,
                probe_vendor_model=True,
            )

        fixed_auth.assert_not_called()
        configured_auth.assert_called_once_with(
            b"target", "B077T_US_13", base_data=b"base", config=config, recovery=False
        )
        engine.flash.assert_awaited_once()

    def test_cli_accepts_only_firmware_and_three_timeouts(self):
        args = ota.build_parser().parse_args(
            [
                "--firmware",
                "fw.bin",
                "--scan-timeout",
                "3",
                "--connect-timeout",
                "4",
                "--operation-timeout",
                "5",
            ]
        )

        self.assertEqual(args.firmware, "fw.bin")
        self.assertEqual(
            (args.scan_timeout, args.connect_timeout, args.operation_timeout),
            (3, 4, 5),
        )

    def test_cli_rejects_every_non_finite_or_non_positive_timeout(self):
        for option in (
            "--scan-timeout",
            "--connect-timeout",
            "--operation-timeout",
        ):
            for value in ("nan", "inf", "0", "-1"):
                with self.subTest(option=option, value=value):
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        status = ota.main(["--firmware", "unused.bin", option, value])

                    self.assertEqual(status, 2)
                    self.assertIn("timeout", stderr.getvalue())


class FakeGattPreflightTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, script):
        return ScriptedGattClient("device-1", timeout=5, script=script)

    async def collect(self, client):
        return await ota.collect_preflight(
            "device-1",
            advertised_name=phase1_ble_info.TARGET_NAME,
            client_factory=lambda device, timeout: client,
            image=approved_image(),
            settle_seconds=0,
            operation_timeout=0.01,
            connect_timeout=5,
        )

    async def test_factory_safe_path_finishes_with_vendor_model_unavailable(self):
        client = self.make_client(normal_script())

        report = await self.collect(client)

        client.assert_complete()
        self.assertEqual(
            client.writes,
            [
                (b"\x10\x00", True),
                (b"\x23\x00", True),
            ],
        )
        self.assertIsNone(report.vendor_ota_model)
        self.assertFalse(report.ready_for_future_flash)
        self.assertIn("Vendor OTA model B077Tを確認できません", report.blockers)

    async def test_installed_global_signature_does_not_send_model_probe_commands(self):
        script = normal_script()
        script[-1] = GattStep("read", INSTALLED_GLOBAL_FW_INFO_FRAME)
        client = self.make_client(script)

        report = await ota.collect_preflight_on_client(
            client,
            advertised_name="Cube Pocket Keyboard 2",
            image=approved_jp_image(),
            settle_seconds=0,
            operation_timeout=0.01,
            accept_installed_global_signature=True,
        )

        client.assert_complete()
        self.assertEqual(
            client.writes,
            [(b"\x10\x00", True), (b"\x23\x00", True)],
        )
        self.assertTrue(report.installed_global_signature_matched)

    async def test_timeout_stops_without_an_additional_write(self):
        script = normal_script()
        script[3] = GattStep("write", b"\x10\x00", response=True, delay=1)
        client = self.make_client(script)

        with self.assertRaises(phase1_ble_info.Phase1Error):
            await self.collect(client)

        self.assertEqual(client.writes, [(b"\x10\x00", True)])

    async def test_disconnect_stops_without_an_additional_write(self):
        script = normal_script()
        script[3] = GattStep(
            "write",
            b"\x10\x00",
            response=True,
            error=ConnectionError("link lost"),
            disconnect=True,
        )
        client = self.make_client(script)

        with self.assertRaises(phase1_ble_info.Phase1Error):
            await self.collect(client)

        self.assertEqual(client.writes, [(b"\x10\x00", True)])

    async def test_invalid_response_stops_without_an_additional_write(self):
        script = normal_script()
        script[4] = GattStep("read", bytes.fromhex("0e 02 11 00"))
        client = self.make_client(script)

        with self.assertRaises(phase1_ble_info.Phase1Error):
            await self.collect(client)

        self.assertEqual(client.writes, [(b"\x10\x00", True)])

    async def test_explicit_vendor_probe_rejects_malformed_model_response_safely(self):
        client = self.make_client(
            normal_script()
            + [
                GattStep("write", b"\x2a\x00", response=True),
                GattStep("read", bytes.fromhex("0e 03 2a 00 01")),
                GattStep("write", b"\x2b\x00\x00\x00\x00", response=True),
                GattStep("read", b"malformed"),
            ]
        )

        with self.assertRaisesRegex(phase1_ble_info.Phase1Error, "Vendor OTA model"):
            await ota.collect_preflight_on_client(
                client,
                advertised_name=phase1_ble_info.TARGET_NAME,
                image=approved_image(),
                settle_seconds=0,
                operation_timeout=1,
                probe_vendor_model=True,
            )

        self.assertEqual(
            client.writes,
            [
                (b"\x10\x00", True),
                (b"\x23\x00", True),
                (b"\x2a\x00", True),
                (b"\x2b\x00\x00\x00\x00", True),
            ],
        )

    async def test_same_session_fw_info_timeout_becomes_phase1_error(self):
        script = normal_script()
        script[5] = GattStep("write", b"\x23\x00", response=True, delay=1)
        client = self.make_client(script)

        with self.assertRaisesRegex(phase1_ble_info.Phase1Error, "タイムアウト"):
            await ota.collect_preflight_on_client(
                client,
                advertised_name=phase1_ble_info.TARGET_NAME,
                image=approved_image(),
                settle_seconds=0,
                operation_timeout=0.01,
            )

        self.assertEqual(
            client.writes,
            [(b"\x10\x00", True), (b"\x23\x00", True)],
        )


class CliDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_uses_observed_advertised_local_name(self):
        device = SimpleNamespace(name=None)
        target = phase1_ble_info.DiscoveredTarget(
            device=device,
            advertised_name="Cube Pocket Keyboard 1",
        )
        expected_report = object()
        args = argparse.Namespace(
            scan_timeout=3,
            connect_timeout=4,
            operation_timeout=5,
        )
        fake_bleak = SimpleNamespace(BleakClient=object, BleakScanner=object)

        with (
            patch.dict(sys.modules, {"bleak": fake_bleak}),
            patch.object(
                phase1_ble_info, "scan_target", AsyncMock(return_value=target)
            ),
            patch.object(
                ota, "collect_preflight", AsyncMock(return_value=expected_report)
            ) as collect,
        ):
            report = await ota._run(args, approved_image())

        self.assertIs(report, expected_report)
        self.assertIs(collect.await_args.args[0], device)
        self.assertEqual(
            collect.await_args.kwargs["advertised_name"],
            "Cube Pocket Keyboard 1",
        )

    async def test_run_execute_forwards_explicit_probe_flags(self):
        target = phase1_ble_info.DiscoveredTarget(
            device=object(), advertised_name="Cube Pocket Keyboard 3"
        )
        client = ScriptedGattClient("device-1", timeout=5, script=[])
        args = argparse.Namespace(
            scan_timeout=3,
            connect_timeout=4,
            operation_timeout=5,
            device_uuid=None,
            probe_vendor_model=True,
            accept_factory_signature=False,
        )
        fake_bleak = SimpleNamespace(BleakClient=object, BleakScanner=object)

        with (
            patch.dict(sys.modules, {"bleak": fake_bleak}),
            patch.object(ota, "_scan_target", AsyncMock(return_value=target)),
            patch.object(
                phase1_ble_info,
                "make_bleak_client_factory",
                return_value=lambda device, timeout: client,
            ),
            patch.object(ota, "execute_on_client", AsyncMock(return_value=object())) as execute,
        ):
            await ota._run_execute(args, approved_image(), b"approved")

        self.assertTrue(execute.await_args.kwargs["probe_vendor_model"])
        self.assertFalse(execute.await_args.kwargs["accept_factory_signature"])


if __name__ == "__main__":
    unittest.main()
