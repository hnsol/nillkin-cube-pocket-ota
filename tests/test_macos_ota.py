import argparse
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests.fakes import GattStep, ScriptedGattClient
from tools import ble_transport
from tools import firmware_image
from tools import macos_ota as ota
from tools import ota_protocol
from tools import phase1_ble_info


FW_INFO_FRAME = bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61")
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
    ]


class PreflightEvaluationTests(unittest.TestCase):
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

    async def test_normal_path_finishes_only_two_read_only_writes(self):
        client = self.make_client(normal_script())

        report = await self.collect(client)

        client.assert_complete()
        self.assertEqual(client.writes, [(b"\x10\x00", True), (b"\x23\x00", True)])
        self.assertFalse(report.ready_for_future_flash)
        self.assertIn("B077T", report.blockers[0])

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


if __name__ == "__main__":
    unittest.main()
