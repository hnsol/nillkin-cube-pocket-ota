import asyncio
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from tools import ble_transport
from tools import ota_protocol as protocol
from tools import phase1_ble_info as ble_info


class FakeCharacteristic:
    def __init__(self, uuid, properties):
        self.uuid = uuid
        self.properties = properties


class FakeService:
    def __init__(self, uuid, characteristics):
        self.uuid = uuid
        self.characteristics = characteristics


class FakeClient:
    def __init__(self, device, *, timeout, services, reads):
        self.device = device
        self.timeout = timeout
        self.services = services
        self._reads = iter(reads)
        self.events = []
        self.is_connected = True

    async def __aenter__(self):
        self.events.append(("connect", self.device))
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append(("disconnect",))

    async def read_gatt_char(self, characteristic):
        self.events.append(("read", ble_info.normalize_uuid(characteristic.uuid)))
        return bytearray(next(self._reads))

    async def write_gatt_char(self, characteristic, data, *, response):
        self.events.append(
            ("write", ble_info.normalize_uuid(characteristic.uuid), bytes(data), response)
        )


def make_services(*, include_device_info=False):
    base = "0000{}-0000-1000-8000-00805f9b34fb"
    services = [
        FakeService(
            base.format("ff00"),
            [
                FakeCharacteristic(
                    base.format("ff01"),
                    ["read", "write", "write-without-response", "notify"],
                ),
                FakeCharacteristic(base.format("ff02"), ["read", "write"]),
                FakeCharacteristic(base.format("ff03"), ["read", "write"]),
            ],
        )
    ]
    if include_device_info:
        services.append(
            FakeService(
                base.format("180a"),
                [
                    FakeCharacteristic(base.format("2A24"), ["read"]),
                    FakeCharacteristic(base.format("2A26"), ["read"]),
                ],
            )
        )
    return services


class UuidTests(unittest.TestCase):
    def test_normalizes_sig_16_bit_and_bluetooth_base_uuid(self):
        self.assertEqual(ble_info.normalize_uuid("FF01"), "ff01")
        self.assertEqual(
            ble_info.normalize_uuid("0000FF01-0000-1000-8000-00805F9B34FB"),
            "ff01",
        )
        self.assertEqual(ble_info.normalize_uuid("2A24"), "2a24")
        self.assertEqual(
            ble_info.normalize_uuid("00002A26-0000-1000-8000-00805F9B34FB"),
            "2a26",
        )

    def test_keeps_non_bluetooth_uuid_in_canonical_lowercase(self):
        self.assertEqual(
            ble_info.normalize_uuid("12345678-1234-5678-9ABC-DEF012345678"),
            "12345678-1234-5678-9abc-def012345678",
        )


class FirmwareInfoTests(unittest.TestCase):
    def test_parses_version_and_little_endian_checksum_like_otautility(self):
        version, checksum = ble_info.parse_fw_info_response(
            bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61")
        )

        self.assertEqual(version, "1.0")
        self.assertEqual(checksum, 0x6162)

    def test_rejects_unexpected_fw_info_frame(self):
        with self.assertRaises(ble_info.Phase1Error) as caught:
            ble_info.parse_fw_info_response(bytes.fromhex("0e 04 24 00 00 00"))

        self.assertIsInstance(caught.exception.__cause__, protocol.ProtocolError)


class DirectExecutionTests(unittest.TestCase):
    def test_script_help_runs_with_protocol_module_available(self):
        project_root = Path(__file__).resolve().parents[1]

        result = subprocess.run(
            [sys.executable, "tools/phase1_ble_info.py", "--help"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


class GattTests(unittest.TestCase):
    def test_requires_ff00_service_and_all_three_characteristics(self):
        discovered = ble_info.inspect_gatt(make_services())

        self.assertEqual(set(discovered), {"ff01", "ff02", "ff03"})
        self.assertEqual(
            discovered["ff01"].properties,
            ("notify", "read", "write", "write-without-response"),
        )

    def test_rejects_missing_required_characteristic(self):
        services = make_services()
        services[0].characteristics.pop()

        with self.assertRaisesRegex(ble_info.GattValidationError, "ff03"):
            ble_info.inspect_gatt(services)

    def test_rejects_ff01_without_required_read_and_write_properties(self):
        services = make_services()
        services[0].characteristics[0].properties = ["notify"]

        with self.assertRaisesRegex(ble_info.GattValidationError, "ff01"):
            ble_info.inspect_gatt(services)

    def test_rejects_ff01_with_only_write_without_response(self):
        services = make_services()
        services[0].characteristics[0].properties = [
            "read",
            "write-without-response",
        ]

        with self.assertRaisesRegex(ble_info.GattValidationError, "ff01"):
            ble_info.inspect_gatt(services)


class Phase1FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_fixed_four_command_read_only_sequence(self):
        model_info = bytes.fromhex(
            "0e 17 2b 00 00 00 42 30 37 37 54 5f 55 53 5f 31 33 00"
            " 00 00 00 00 00 00 00"
        )
        client = FakeClient(
            "device-1",
            timeout=5.0,
            services=make_services(),
            reads=[
                b"BOOT",
                bytes.fromhex("0e 02 10 00"),
                bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61"),
                bytes.fromhex("0e 03 2a 00 01"),
                model_info,
            ],
        )

        result = await ble_info.collect_phase1_info(
            "device-1",
            client_factory=lambda device, timeout: client,
            settle_seconds=0,
            operation_timeout=1,
            connect_timeout=5,
        )

        self.assertEqual(result.initial_read, b"BOOT")
        self.assertEqual(result.ota_init_response, bytes.fromhex("0e 02 10 00"))
        self.assertEqual(
            result.fw_info_response,
            bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61"),
        )
        self.assertIsNone(result.model_number)
        self.assertIsNone(result.firmware_revision)
        self.assertEqual(result.model_count_response, bytes.fromhex("0e 03 2a 00 01"))
        self.assertEqual(result.model_info_response, model_info)
        self.assertEqual(result.model_identity, "B077T_US_13")
        self.assertEqual(
            client.events,
            [
                ("connect", "device-1"),
                ("read", "ff01"),
                ("write", "ff01", b"\x10\x00", True),
                ("read", "ff01"),
                ("write", "ff01", b"\x23\x00", True),
                ("read", "ff01"),
                ("write", "ff01", b"\x2a\x00", True),
                ("read", "ff01"),
                ("write", "ff01", b"\x2b\x00\x00\x00\x00", True),
                ("read", "ff01"),
                ("disconnect",),
            ],
        )

    async def test_reads_standard_model_and_firmware_revision_when_present(self):
        client = FakeClient(
            "device-1",
            timeout=5.0,
            services=make_services(include_device_info=True),
            reads=[
                b"PAR2801\x00",
                b"1.0.0\x00",
                b"BOOT",
                bytes.fromhex("0e 02 10 00"),
                bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61"),
                bytes.fromhex("0e 03 2a 00 01"),
                bytes.fromhex(
                    "0e 17 2b 00 00 00 42 30 37 37 54 5f 55 53 5f 31 33 00"
                    " 00 00 00 00 00 00 00"
                ),
            ],
        )

        result = await ble_info.collect_phase1_info(
            "device-1",
            client_factory=lambda device, timeout: client,
            settle_seconds=0,
            operation_timeout=1,
            connect_timeout=5,
        )

        self.assertEqual(result.model_number, "PAR2801")
        self.assertEqual(result.firmware_revision, "1.0.0")
        self.assertEqual(
            client.events,
            [
                ("connect", "device-1"),
                ("read", "2a24"),
                ("read", "2a26"),
                ("read", "ff01"),
                ("write", "ff01", b"\x10\x00", True),
                ("read", "ff01"),
                ("write", "ff01", b"\x23\x00", True),
                ("read", "ff01"),
                ("write", "ff01", b"\x2a\x00", True),
                ("read", "ff01"),
                ("write", "ff01", b"\x2b\x00\x00\x00\x00", True),
                ("read", "ff01"),
                ("disconnect",),
            ],
        )

    async def test_zero_model_count_fails_before_model_info_write(self):
        client = FakeClient(
            "device-1",
            timeout=5.0,
            services=make_services(),
            reads=[
                b"BOOT",
                bytes.fromhex("0e 02 10 00"),
                bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61"),
                bytes.fromhex("0e 03 2a 00 00"),
            ],
        )

        with self.assertRaises(ble_info.Phase1Error):
            await ble_info.collect_phase1_info(
                "device-1",
                client_factory=lambda device, timeout: client,
                settle_seconds=0,
                operation_timeout=1,
                connect_timeout=5,
            )

        self.assertEqual(
            [event[2] for event in client.events if event[0] == "write"],
            [b"\x10\x00", b"\x23\x00", b"\x2a\x00"],
        )

    async def test_operation_timeout_is_reported_as_phase1_error(self):
        class HangingClient(FakeClient):
            async def read_gatt_char(self, characteristic):
                await asyncio.sleep(1)

        client = HangingClient(
            "device-1", timeout=5.0, services=make_services(), reads=[]
        )

        with self.assertRaisesRegex(ble_info.Phase1Error, "タイムアウト"):
            await ble_info.collect_phase1_info(
                "device-1",
                client_factory=lambda device, timeout: client,
                settle_seconds=0,
                operation_timeout=0.01,
                connect_timeout=5,
            )

    async def test_invalid_vendor_response_is_rejected_by_transport(self):
        client = FakeClient(
            "device-1",
            timeout=5.0,
            services=make_services(),
            reads=[b"BOOT", bytes.fromhex("0e 02 11 00")],
        )

        with self.assertRaises(ble_info.Phase1Error) as caught:
            await ble_info.collect_phase1_info(
                "device-1",
                client_factory=lambda device, timeout: client,
                settle_seconds=0,
                operation_timeout=1,
                connect_timeout=5,
            )

        self.assertIsInstance(
            caught.exception.__cause__, ble_transport.InvalidResponseError
        )


class Phase1ResultCompatibilityTests(unittest.TestCase):
    def test_seventh_positional_argument_remains_model_identity(self):
        result = ble_info.Phase1Result(
            {},
            b"initial",
            b"init",
            b"fw",
            "PAR2801",
            "1.0.0",
            "B077T_US_13",
        )

        self.assertEqual(result.model_identity, "B077T_US_13")
        self.assertEqual(result.model_count_response, b"")
        self.assertEqual(result.model_info_response, b"")


class ClientFactoryTests(unittest.TestCase):
    def test_macos_client_factory_does_not_pass_start_notify_options(self):
        calls = []

        class Client:
            def __init__(self, device, **kwargs):
                calls.append((device, kwargs))

        factory = ble_info.make_bleak_client_factory(Client)

        factory("device-1", timeout=5)

        self.assertEqual(calls, [("device-1", {"timeout": 5})])


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_scans_for_each_exact_allowlisted_device_name(self):
        class Scanner:
            calls = []
            candidate = None

            @classmethod
            async def find_device_by_filter(cls, filterfunc, timeout):
                cls.calls.append(timeout)
                advertisement = SimpleNamespace(local_name=cls.advertised_name)
                return cls.candidate if filterfunc(cls.candidate, advertisement) else None

        for name in ble_info.TARGET_NAMES:
            with self.subTest(name=name):
                Scanner.candidate = SimpleNamespace(name="stale cached name")
                Scanner.advertised_name = name
                target = await ble_info.scan_target(Scanner, timeout=8.0)

                self.assertIs(target.device, Scanner.candidate)
                self.assertEqual(target.advertised_name, name)

        self.assertEqual(Scanner.calls, [8.0, 8.0, 8.0])

    async def test_scan_rejects_name_outside_exact_allowlist(self):
        class Scanner:
            @classmethod
            async def find_device_by_filter(cls, filterfunc, timeout):
                device = SimpleNamespace(name="Cube Pocket Keyboard 3")
                advertisement = SimpleNamespace(local_name="Cube Pocket Keyboard 30")
                return device if filterfunc(device, advertisement) else None

        with self.assertRaises(ble_info.TargetNotFoundError):
            await ble_info.scan_target(Scanner, timeout=0.01)

    async def test_reports_target_not_found(self):
        class Scanner:
            @classmethod
            async def find_device_by_filter(cls, filterfunc, timeout):
                return None

        with self.assertRaisesRegex(
            ble_info.TargetNotFoundError, "Cube Pocket Keyboard 1"
        ):
            await ble_info.scan_target(Scanner, timeout=0.01)

    async def test_wraps_scanner_backend_failure_as_phase1_error(self):
        class Scanner:
            @classmethod
            async def find_device_by_filter(cls, filterfunc, timeout):
                raise RuntimeError("Bluetooth is not available")

        with self.assertRaisesRegex(ble_info.Phase1Error, "scanに失敗") as caught:
            await ble_info.scan_target(Scanner, timeout=1.0)

        self.assertIsInstance(caught.exception.__cause__, RuntimeError)


class OutputTests(unittest.TestCase):
    def test_prints_raw_hex_and_ascii_candidates_without_guessing_fields(self):
        result = ble_info.Phase1Result(
            characteristics=ble_info.inspect_gatt(make_services()),
            initial_read=b"\x01BOOT\x00",
            ota_init_response=b"\x10\x00",
            fw_info_response=bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61"),
            model_number="PAR2801",
            firmware_revision="1.0.0",
            model_count_response=bytes.fromhex("0e 03 2a 00 01"),
            model_info_response=bytes.fromhex(
                "0e 17 2b 00 00 00 42 30 37 37 54 5f 55 53 5f 31 33 00"
                " 00 00 00 00 00 00 00"
            ),
            model_identity="B077T_US_13",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            ble_info.print_result(result)

        rendered = output.getvalue()
        self.assertIn("ff01", rendered)
        self.assertIn("0e 09 23 00 31 2e 30 00 00 62 61", rendered)
        self.assertIn("GATT Model Number (2a24): PAR2801", rendered)
        self.assertIn("GATT Firmware Revision (2a26): 1.0.0", rendered)
        self.assertIn("OTA Firmware Version: 1.0", rendered)
        self.assertIn("OTA checksum: 0x6162", rendered)
        self.assertIn("Vendor OTA Model: B077T_US_13", rendered)
        self.assertIn("0e 03 2a 00 01", rendered)
        self.assertIn("0e 17 2b 00", rendered)

    def test_prints_none_when_standard_device_info_is_absent(self):
        result = ble_info.Phase1Result(
            characteristics=ble_info.inspect_gatt(make_services()),
            initial_read=b"",
            ota_init_response=b"",
            fw_info_response=b"",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            ble_info.print_result(result)

        rendered = output.getvalue()
        self.assertIn("GATT Model Number (2a24): None", rendered)
        self.assertIn("GATT Firmware Revision (2a26): None", rendered)


if __name__ == "__main__":
    unittest.main()
