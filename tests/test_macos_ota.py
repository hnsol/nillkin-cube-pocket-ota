import argparse
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.fakes import GattStep, ScriptedGattClient
from tools import ble_transport
from tools import firmware_image
from tools import macos_ota as ota
from tools import ota_protocol
from tools import phase1_ble_info


FW_INFO_FRAME = bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61")
MODEL_INFO_FRAME = bytes.fromhex(
    "0e 17 2b 00 00 00 42 30 37 37 54 5f 55 53 5f 31 33 00"
    " 00 00 00 00 00 00 00"
)
EXPECTED_SERVICES = ("ff00", "ff01", "ff02", "ff03")


def approved_image() -> firmware_image.ValidatedImage:
    return firmware_image.ValidatedImage(
        profile=firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.GLOBAL],
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


def normal_script() -> list[GattStep]:
    return [
        GattStep("read", b"PAR2801\x00"),
        GattStep("read", b"1.0.0\x00"),
        GattStep("read", b"BOOT"),
        GattStep("write", b"\x10\x00", response=True),
        GattStep("read", bytes.fromhex("0e 02 10 00")),
        GattStep("write", b"\x23\x00", response=True),
        GattStep("read", FW_INFO_FRAME),
        GattStep("write", b"\x2a\x00", response=True),
        GattStep("read", bytes.fromhex("0e 03 2a 00 01")),
        GattStep("write", b"\x2b\x00\x00\x00\x00", response=True),
        GattStep("read", MODEL_INFO_FRAME),
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


class ParserSafetyTests(unittest.TestCase):
    def test_cli_has_no_execute_or_state_changing_options(self):
        parser = ota.build_parser()

        for option in ("--execute", "--erase", "--reset", "--chunk-size", "--resume"):
            with self.subTest(option=option), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["--firmware", "fw.bin", option])

        help_text = parser.format_help().lower()
        for term in ("execute", "erase", "reset", "chunk", "resume"):
            self.assertNotIn(term, help_text)

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
                        status = ota.main(
                            ["--firmware", "unused.bin", option, value]
                        )

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

    async def test_normal_path_propagates_vendor_model_through_preflight(self):
        client = self.make_client(normal_script())

        report = await self.collect(client)

        client.assert_complete()
        self.assertEqual(
            client.writes,
            [
                (b"\x10\x00", True),
                (b"\x23\x00", True),
                (b"\x2a\x00", True),
                (b"\x2b\x00\x00\x00\x00", True),
            ],
        )
        self.assertEqual(report.vendor_ota_model, "B077T_US_13")
        self.assertTrue(report.ready_for_future_flash)

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

    async def test_malformed_model_count_stops_before_model_info_write(self):
        script = normal_script()
        script[8] = GattStep("read", bytes.fromhex("0e 03 2a 00 00"))
        client = self.make_client(script)

        with self.assertRaises(phase1_ble_info.Phase1Error):
            await self.collect(client)

        self.assertEqual(
            client.writes,
            [
                (b"\x10\x00", True),
                (b"\x23\x00", True),
                (b"\x2a\x00", True),
            ],
        )

    async def test_malformed_model_count_envelope_stops_before_model_info_write(self):
        script = normal_script()
        script[8] = GattStep("read", bytes.fromhex("0e 02 2a 00"))
        client = self.make_client(script)

        with self.assertRaises(phase1_ble_info.Phase1Error):
            await self.collect(client)

        self.assertEqual(
            client.writes,
            [
                (b"\x10\x00", True),
                (b"\x23\x00", True),
                (b"\x2a\x00", True),
            ],
        )

    async def test_model_count_read_disconnect_stops_before_model_info_write(self):
        script = normal_script()
        script[8] = GattStep(
            "read",
            error=ConnectionError("link lost"),
            disconnect=True,
        )
        client = self.make_client(script)

        with self.assertRaises(phase1_ble_info.Phase1Error):
            await self.collect(client)

        self.assertEqual(
            client.writes,
            [
                (b"\x10\x00", True),
                (b"\x23\x00", True),
                (b"\x2a\x00", True),
            ],
        )

    async def test_model_count_timeout_stops_before_model_info_write(self):
        script = normal_script()
        script[7] = GattStep("write", b"\x2a\x00", response=True, delay=1)
        client = self.make_client(script)

        with self.assertRaises(phase1_ble_info.Phase1Error):
            await self.collect(client)

        self.assertEqual(
            client.writes,
            [
                (b"\x10\x00", True),
                (b"\x23\x00", True),
                (b"\x2a\x00", True),
            ],
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


if __name__ == "__main__":
    unittest.main()
