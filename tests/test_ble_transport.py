import asyncio
import dataclasses
import unittest

from tools import ble_transport as ble
from tools import ota_protocol as protocol


FW_INFO_FRAME = bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61")


class FakeClient:
    def __init__(self, reads):
        self._reads = iter(reads)
        self.writes = []
        self.is_connected = True

    async def write_gatt_char(self, characteristic, data, *, response):
        self.writes.append((bytes(data), response))

    async def read_gatt_char(self, characteristic):
        return next(self._reads)


class ExchangeSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_uses_explicit_write_with_response(self):
        client = FakeClient([FW_INFO_FRAME])
        transport = ble.BleTransport(
            client, "ff01", settle_seconds=0, operation_timeout=1
        )

        result = await transport.exchange(protocol.READ_ONLY_COMMANDS[0x23])

        self.assertEqual(client.writes, [(b"\x23\x00", True)])
        self.assertEqual(result.raw, FW_INFO_FRAME)

    async def test_exchange_rejects_unregistered_command_even_if_shape_is_valid(self):
        client = FakeClient([])
        transport = ble.BleTransport(
            client, "ff01", settle_seconds=0, operation_timeout=1
        )
        unknown = protocol.CommandSpec(
            0x2B, b"\x2b\x00", protocol.WriteMode.WITH_RESPONSE, 4
        )

        with self.assertRaises(ble.UnsafeCommandError):
            await transport.exchange(unknown)

    async def test_exchange_accepts_an_equivalent_registered_spec(self):
        client = FakeClient([FW_INFO_FRAME])
        transport = ble.BleTransport(
            client, "ff01", settle_seconds=0, operation_timeout=1
        )
        equivalent = protocol.CommandSpec(
            0x23, b"\x23\x00", protocol.WriteMode.WITH_RESPONSE, 11
        )

        result = await transport.exchange(equivalent)

        self.assertEqual(result.raw, FW_INFO_FRAME)


class ExchangeFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_rejects_silent_disconnect_after_buffered_read(self):
        class SilentlyDisconnectedClient(FakeClient):
            async def read_gatt_char(self, characteristic):
                response = await super().read_gatt_char(characteristic)
                self.is_connected = False
                return response

        client = SilentlyDisconnectedClient([FW_INFO_FRAME])
        transport = ble.BleTransport(
            client, "ff01", settle_seconds=0, operation_timeout=1
        )

        with self.assertRaises(ble.DisconnectedError):
            await transport.exchange(protocol.READ_ONLY_COMMANDS[0x23])

    async def test_exchange_converts_timeout_to_transport_timeout(self):
        class HangingClient(FakeClient):
            async def write_gatt_char(self, characteristic, data, *, response):
                await asyncio.sleep(1)

        transport = ble.BleTransport(
            HangingClient([]), "ff01", settle_seconds=0, operation_timeout=0.01
        )

        with self.assertRaisesRegex(
            ble.TransportTimeoutError, r"write 0x23 timed out"
        ):
            await transport.exchange(protocol.READ_ONLY_COMMANDS[0x23])

    async def test_exchange_converts_disconnect_to_disconnected_error(self):
        class DisconnectedClient(FakeClient):
            async def write_gatt_char(self, characteristic, data, *, response):
                self.is_connected = False
                raise ConnectionError("link lost")

        transport = ble.BleTransport(
            DisconnectedClient([]), "ff01", settle_seconds=0, operation_timeout=1
        )

        with self.assertRaises(ble.DisconnectedError):
            await transport.exchange(protocol.READ_ONLY_COMMANDS[0x23])

    async def test_exchange_converts_protocol_error_to_invalid_response(self):
        client = FakeClient([bytes.fromhex("0e 02 10 00")])
        transport = ble.BleTransport(
            client, "ff01", settle_seconds=0, operation_timeout=1
        )

        with self.assertRaises(ble.InvalidResponseError) as caught:
            await transport.exchange(protocol.READ_ONLY_COMMANDS[0x23])

        self.assertIsInstance(caught.exception.__cause__, protocol.ProtocolError)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_notification_discriminator_accepts_only_vendor_frames(self):
        self.assertTrue(ble.is_expected_notification(bytes.fromhex("0e 02 10 00")))
        self.assertFalse(ble.is_expected_notification(b"PAR2801"))

    async def test_notification_options_match_start_notify_cb_api(self):
        calls = []

        class Client:
            async def start_notify(self, characteristic, callback, *, cb):
                calls.append((characteristic, callback, cb))

        callback = object()
        await Client().start_notify(
            "ff01", callback, **ble.corebluetooth_notification_options()
        )

        self.assertEqual(calls[0][0:2], ("ff01", callback))
        self.assertEqual(
            calls[0][2],
            {"notification_discriminator": ble.is_expected_notification},
        )


class IdentityTests(unittest.TestCase):
    def test_identity_excludes_core_bluetooth_identifier(self):
        identity = ble.build_identity(
            advertised_name="Cube Pocket Keyboard 3",
            gatt_model="PAR2801",
            gatt_revision="1.0.0",
            service_uuids=("ff00",),
        )

        fields = dataclasses.asdict(identity)
        self.assertNotIn("identifier", fields)
        self.assertEqual(
            fields,
            {
                "advertised_name": "Cube Pocket Keyboard 3",
                "gatt_model": "PAR2801",
                "gatt_revision": "1.0.0",
                "service_uuids": ("ff00",),
            },
        )


if __name__ == "__main__":
    unittest.main()
