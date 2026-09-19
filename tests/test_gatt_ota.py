import asyncio
import gc
import struct
import unittest
import warnings
from collections import defaultdict, deque
from types import SimpleNamespace
from unittest import mock

from tools import firmware_image, gatt_ota, keymap_config


def validated_image(
    kind: firmware_image.ImageKind = firmware_image.ImageKind.GLOBAL,
) -> firmware_image.ValidatedImage:
    return firmware_image.ValidatedImage(
        profile=firmware_image.FirmwareProfile(
            kind=kind,
            size=0,
            sha256="synthetic",
            full_file_sum16=0,
            embedded_version=b"B077T_TEST",
            hardware_model=b"PAR2801",
            keymap_marker_offset=0,
        ),
        keymap=(),
    )


def authorize(
    data: bytes,
    *,
    recovery: bool = False,
    kind: firmware_image.ImageKind = firmware_image.ImageKind.GLOBAL,
):
    with mock.patch.object(
        gatt_ota.firmware_image,
        "validate_image",
        return_value=validated_image(kind),
    ):
        return gatt_ota.authorize_firmware(data, "B077T_TEST", recovery=recovery)


def init_response(
    *,
    offset: int = 0,
    checksum: int = 0,
    max_object_size: int = 4,
    mtu_size: int = 4,
    prn_threshold: int = 2,
) -> bytes:
    return b"\x0e\x10\x27\x00\x01" + struct.pack(
        "<HHIHHB",
        offset,
        checksum,
        max_object_size,
        mtu_size,
        prn_threshold,
        1,
    )


class AuthorizationTests(unittest.TestCase):
    def test_authorization_revalidates_and_binds_data_profile_and_mode(self):
        data = b"synthetic"
        validated = validated_image()
        with mock.patch.object(
            gatt_ota.firmware_image,
            "validate_image",
            return_value=validated,
        ) as validate:
            authorized = gatt_ota.authorize_firmware(
                data, "B077T_FACTORY", recovery=False
            )

        validate.assert_called_once_with(data)
        self.assertIs(authorized.data, data)
        self.assertIs(authorized.profile, validated.profile)
        self.assertEqual(authorized.mode.value, "flash")

    def test_authorization_rejects_non_b077t_model_after_revalidation(self):
        data = b"synthetic"
        with (
            mock.patch.object(
                gatt_ota.firmware_image,
                "validate_image",
                return_value=validated_image(),
            ) as validate,
            self.assertRaisesRegex(gatt_ota.GattProtocolError, "B077T"),
        ):
            gatt_ota.authorize_firmware(data, "OTHER", recovery=False)

        validate.assert_called_once_with(data)

    def test_recovery_authorization_accepts_global_only(self):
        data = b"synthetic"
        with (
            mock.patch.object(
                gatt_ota.firmware_image,
                "validate_image",
                return_value=validated_image(firmware_image.ImageKind.JP_LANG),
            ),
            self.assertRaisesRegex(gatt_ota.GattProtocolError, "GLOBAL"),
        ):
            gatt_ota.authorize_firmware(data, "B077T_TEST", recovery=True)

    def test_configured_authorization_revalidates_base_and_config_and_never_recovers(
        self,
    ):
        data = b"configured-target"
        base = b"approved-global"
        config = keymap_config.KeymapConfig({"caps_lock": 0xE0})
        configured = validated_image(firmware_image.ImageKind.CONFIGURED)
        with (
            mock.patch.object(
                gatt_ota.phase3_build_patch,
                "validate_configured_target",
                return_value=configured,
            ) as validate,
            self.assertRaisesRegex(gatt_ota.GattProtocolError, "recovery"),
        ):
            gatt_ota.authorize_configured_firmware(
                data, "B077T_TEST", base_data=base, config=config, recovery=True
            )

        self.assertEqual(validate.call_count, 0)
        with mock.patch.object(
            gatt_ota.phase3_build_patch,
            "validate_configured_target",
            return_value=configured,
        ) as validate:
            authorized = gatt_ota.authorize_configured_firmware(
                data, "B077T_TEST", base_data=base, config=config, recovery=False
            )

        validate.assert_called_once_with(base, data, config)
        self.assertEqual(authorized.profile.kind, firmware_image.ImageKind.CONFIGURED)
        self.assertEqual(authorized.mode, gatt_ota.AuthorizationMode.FLASH)


class FakeGattClient:
    def __init__(
        self,
        *,
        reads: list[bytes],
        notify_after_write: dict[bytes, list[bytes]] | None = None,
        notify_burst_after_write: dict[bytes, list[bytes]] | None = None,
        mtu_size: int = 247,
    ) -> None:
        self.is_connected = True
        self.mtu_size = mtu_size
        self.events: list[tuple] = []
        self._reads = deque(reads)
        self._notifications = defaultdict(deque)
        for payload, frames in (notify_after_write or {}).items():
            self._notifications[payload].extend(frames)
        self._notification_bursts = dict(notify_burst_after_write or {})
        self._callback = None

    async def start_notify(self, characteristic, callback, **options):
        self.events.append(("start-notify", characteristic, options))
        self._callback = callback

    async def stop_notify(self, characteristic):
        self.events.append(("stop-notify", characteristic))

    async def write_gatt_char(self, characteristic, data, *, response):
        payload = bytes(data)
        self.events.append(("write", characteristic, payload, response))
        frames = list(self._notification_bursts.pop(payload, ()))
        if self._notifications[payload]:
            frames.append(self._notifications[payload].popleft())
        if response:
            for frame in frames:
                self._callback(characteristic, frame)
        elif frames:
            def deliver_notifications():
                for frame in frames:
                    self._callback(characteristic, frame)

            asyncio.get_running_loop().call_later(0, deliver_notifications)

    async def read_gatt_char(self, characteristic):
        self.events.append(("read", characteristic))
        return self._reads.popleft()


class StopBlockingGattClient(FakeGattClient):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stop_started = asyncio.Event()
        self._never = asyncio.Event()

    async def stop_notify(self, characteristic):
        self.events.append(("stop-notify", characteristic))
        self.stop_started.set()
        await self._never.wait()


class FailingStopGattClient(FakeGattClient):
    async def stop_notify(self, characteristic):
        self.events.append(("stop-notify", characteristic))
        raise RuntimeError("stop notify failed")


class SlowGattClient(FakeGattClient):
    def __init__(self, *, slow_operation: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._slow_operation = slow_operation

    async def start_notify(self, characteristic, callback, **options):
        if self._slow_operation == "start-notify":
            await asyncio.sleep(1)
        await super().start_notify(characteristic, callback, **options)

    async def write_gatt_char(self, characteristic, data, *, response):
        if self._slow_operation == "write":
            await asyncio.sleep(1)
        await super().write_gatt_char(characteristic, data, response=response)

    async def read_gatt_char(self, characteristic):
        if self._slow_operation == "read":
            await asyncio.sleep(1)
        return await super().read_gatt_char(characteristic)


class SlowPayloadGattClient(FakeGattClient):
    async def write_gatt_char(self, characteristic, data, *, response):
        if not response:
            await asyncio.sleep(1)
        await super().write_gatt_char(characteristic, data, response=response)


class DelayedUpgradeAckGattClient(FakeGattClient):
    def __init__(self, *, upgrade: bytes, upgrade_ack: bytes, delay: float, **kwargs):
        super().__init__(**kwargs)
        self._upgrade = upgrade
        self._upgrade_ack = upgrade_ack
        self._delay = delay

    async def write_gatt_char(self, characteristic, data, *, response):
        await super().write_gatt_char(characteristic, data, response=response)
        if response and bytes(data) == self._upgrade:
            asyncio.get_running_loop().call_later(
                self._delay, self._callback, characteristic, self._upgrade_ack
            )


class AckBeforeWriteReturnsGattClient(FakeGattClient):
    def __init__(self, *, final_payload: bytes, ack: bytes, **kwargs):
        super().__init__(**kwargs)
        self._final_payload = final_payload
        self._ack = ack

    async def write_gatt_char(self, characteristic, data, *, response):
        payload = bytes(data)
        if not response and payload == self._final_payload:
            self.events.append(("write", characteristic, payload, response))
            self._callback(characteristic, self._ack)
            await asyncio.sleep(0)
            return
        await super().write_gatt_char(characteristic, data, response=response)


class ObjectEndChecksumGattClient(FakeGattClient):
    def __init__(self, *, final_ack: bytes | None, **kwargs):
        super().__init__(**kwargs)
        self._final_ack = final_ack
        self._payload_bytes_dispatched = 0
        self._early_ack_sent = False

    async def write_gatt_char(self, characteristic, data, *, response):
        await super().write_gatt_char(characteristic, data, response=response)
        if response or bytes(data) == b"\x22\x00":
            return
        self._payload_bytes_dispatched += len(data)
        if not self._early_ack_sent and self._payload_bytes_dispatched >= 3313:
            self._early_ack_sent = True
            self._callback(characteristic, bytes.fromhex("17 00 5c 3d"))
        if self._payload_bytes_dispatched == 4096 and self._final_ack is not None:
            self._callback(characteristic, self._final_ack)


class FirstStartBlockingGattClient(FakeGattClient):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.first_start = asyncio.Event()
        self._start_calls = 0
        self._never = asyncio.Event()

    async def start_notify(self, characteristic, callback, **options):
        self._start_calls += 1
        self.events.append(("start-notify", characteristic, options))
        self._callback = callback
        if self._start_calls == 1:
            self.first_start.set()
            await self._never.wait()


class DisconnectingGattClient(FakeGattClient):
    async def write_gatt_char(self, characteristic, data, *, response):
        await super().write_gatt_char(characteristic, data, response=response)
        if bytes(data).startswith(b"\x25"):
            self.is_connected = False


class DisconnectOnResetGattClient(FakeGattClient):
    async def write_gatt_char(self, characteristic, data, *, response):
        await super().write_gatt_char(characteristic, data, response=response)
        if bytes(data) == b"\x22\x00":
            self.is_connected = False


class RaiseOnResetGattClient(FakeGattClient):
    async def write_gatt_char(self, characteristic, data, *, response):
        if bytes(data) == b"\x22\x00":
            self.events.append(("write", characteristic, bytes(data), response))
            self.is_connected = False
            raise ConnectionError("reset write failed")
        await super().write_gatt_char(characteristic, data, response=response)


class CoreBluetoothPeripheral:
    def __init__(self, ready: list[object]) -> None:
        self.ready = deque(ready)
        self.events: list[tuple] = []

    def canSendWriteWithoutResponse(self):
        value = self.ready.popleft() if self.ready else True
        self.events.append(("ready", value))
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value()
        return value

class CoreBluetoothGattClient(FakeGattClient):
    backend_id = "core_bluetooth"

    def __init__(self, *, ready: list[object], **kwargs) -> None:
        super().__init__(**kwargs)
        self.peripheral = CoreBluetoothPeripheral(ready)
        self._backend = SimpleNamespace(_peripheral=self.peripheral)

    async def write_gatt_char(self, characteristic, data, *, response):
        self.peripheral.events.append(("write", bytes(data), response))
        await super().write_gatt_char(characteristic, data, response=response)


class StaleAckBeforePayloadTaskEngine(gatt_ota.GattOtaEngine):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._injected_stale_ack = False

    async def _bounded(self, awaitable, stage, timeout=None):
        if stage.startswith("write payload fragment") and not self._injected_stale_ack:
            self._injected_stale_ack = True
            self._notification_callback("ff01", bytearray.fromhex("17 0300"))
        return await super()._bounded(awaitable, stage, timeout)


def small_transfer_client(
    *,
    object_ack: bytes = bytes.fromhex("25 000000"),
    checksum_ack: bytes = bytes.fromhex("17 0300"),
    upgrade_ack: bytes = bytes.fromhex("18 0000"),
) -> FakeGattClient:
    firmware = b"\x01\x02"
    upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
    return FakeGattClient(
        reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
        notify_after_write={
            bytes.fromhex("25 00000000 04000000"): [object_ack],
            firmware: [checksum_ack],
            upgrade: [upgrade_ack],
        },
    )


class NormalTransferTests(unittest.IsolatedAsyncioTestCase):
    def test_physical_fragment_size_is_largest_aligned_transport_limit(self):
        state = gatt_ota.pixart_ota.OtaState(0, 1, 0, 0, 4096, 244, 16, 1)

        for host_mtu, expected in ((50, 44), (23, 20), (247, 244)):
            with self.subTest(host_mtu=host_mtu):
                engine = gatt_ota.GattOtaEngine(
                    FakeGattClient(reads=[], mtu_size=host_mtu)
                )
                self.assertEqual(engine._validate_payload_transport(state), expected)

    def test_physical_fragment_limit_below_four_fails_closed(self):
        state = gatt_ota.pixart_ota.OtaState(0, 1, 0, 0, 4096, 244, 16, 1)
        engine = gatt_ota.GattOtaEngine(FakeGattClient(reads=[], mtu_size=6))

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError, "aligned physical fragment"
        ):
            engine._validate_payload_transport(state)

    async def test_corebluetooth_payload_waits_for_native_wnr_queue(self):
        client = CoreBluetoothGattClient(
            ready=[False, False, True],
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")],
                b"\x01\x02": [bytes.fromhex("17 0300")],
                bytes.fromhex("18 02000000 0300 312e302e31"): [
                    bytes.fromhex("18 0000")
                ],
            },
        )
        engine = gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            chunk_pacing_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.1,
        )

        await engine.flash(authorize(b"\x01\x02"))

        payload_write = ("write", b"\x01\x02", False)
        first_ready = client.peripheral.events.index(("ready", False))
        self.assertEqual(
            client.peripheral.events[first_ready : first_ready + 4],
            [("ready", False), ("ready", False), ("ready", True), payload_write],
        )
        self.assertEqual(engine.wnr_ready_false_count, 2)
        self.assertGreater(engine.wnr_ready_wait_seconds, 0)

    async def test_corebluetooth_wnr_queue_timeout_stops_before_payload(self):
        client = CoreBluetoothGattClient(
            ready=[False] * 100,
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaisesRegex(
            gatt_ota.GattTimeoutError,
            r"WNR readiness false observations=[1-9][0-9]*.*WNR readiness.*timed out",
        ):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                chunk_pacing_seconds=0,
                operation_timeout=0.005,
                ack_timeout=0.1,
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(b"\x01\x02", writes)
        self.assertFalse(any(payload.startswith(b"\x18") for payload in writes))
        self.assertNotIn(b"\x22\x00", writes)

    async def test_corebluetooth_wnr_readiness_exception_fails_closed(self):
        client = CoreBluetoothGattClient(
            ready=[RuntimeError("native readiness failed")],
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaisesRegex(gatt_ota.GattOtaError, "native readiness failed"):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

        self.assertNotIn(b"\x01\x02", [e[2] for e in client.events if e[0] == "write"])

    async def test_corebluetooth_disconnect_during_wnr_readiness_fails_closed(self):
        client = CoreBluetoothGattClient(
            ready=[],
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")]
            },
        )

        def disconnect():
            client.is_connected = False
            return False

        client.peripheral.ready.append(disconnect)

        with self.assertRaises(gatt_ota.GattDisconnectedError):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

    async def test_corebluetooth_reset_also_waits_for_native_wnr_queue(self):
        client = CoreBluetoothGattClient(
            ready=[True, False, True],
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")],
                b"\x01\x02": [bytes.fromhex("17 0300")],
                bytes.fromhex("18 02000000 0300 312e302e31"): [
                    bytes.fromhex("18 0000")
                ],
            },
        )

        await gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            chunk_pacing_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.1,
        ).flash(authorize(b"\x01\x02"))

        reset_index = client.peripheral.events.index(("write", b"\x22\x00", False))
        self.assertEqual(
            client.peripheral.events[reset_index - 2 : reset_index],
            [("ready", False), ("ready", True)],
        )

    async def test_corebluetooth_backend_without_private_path_fails_closed(self):
        for missing_attribute in ("_backend", "_peripheral"):
            with self.subTest(missing_attribute=missing_attribute):
                client = small_transfer_client()
                client.backend_id = "core_bluetooth"
                if missing_attribute == "_peripheral":
                    client._backend = SimpleNamespace()

                with self.assertRaisesRegex(
                    gatt_ota.GattOtaError, "WNR readiness"
                ):
                    await gatt_ota.GattOtaEngine(
                        client,
                        settle_seconds=0,
                        operation_timeout=0.1,
                        ack_timeout=0.1,
                    ).flash(authorize(b"\x01\x02"))

                self.assertNotIn(
                    b"\x01\x02", [e[2] for e in client.events if e[0] == "write"]
                )

    async def test_corebluetooth_readiness_deadline_precedes_late_true(self):
        client = CoreBluetoothGattClient(
            ready=[False, True],
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaisesRegex(gatt_ota.GattTimeoutError, "WNR readiness"):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                chunk_pacing_seconds=0,
                operation_timeout=0.001,
                ack_timeout=0.1,
            ).flash(authorize(b"\x01\x02"))

        self.assertEqual(list(client.peripheral.ready), [True])
        self.assertNotIn(b"\x01\x02", [e[2] for e in client.events if e[0] == "write"])

    async def test_corebluetooth_existing_peripheral_with_invalid_readiness_fails_closed(self):
        client = small_transfer_client()
        client.backend_id = "core_bluetooth"
        client._backend = SimpleNamespace(_peripheral=SimpleNamespace())

        with self.assertRaisesRegex(gatt_ota.GattOtaError, "WNR readiness"):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

        self.assertNotIn(b"\x01\x02", [e[2] for e in client.events if e[0] == "write"])

    async def test_host_at_least_device_mtu_keeps_device_payload_chunks(self):
        firmware = b"\x01\x02\x03\x04"
        upgrade = bytes.fromhex("18 04000000 0a00 312e302e31")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=2)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")],
                firmware: [bytes.fromhex("17 0a00")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )

        await gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            chunk_pacing_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.1,
        ).flash(authorize(firmware))

        payload_writes = [
            event[2]
            for event in client.events
            if event[0] == "write"
            and event[3] is False
            and event[2] != b"\x22\x00"
        ]
        self.assertEqual(payload_writes, [firmware])
        self.assertIn(("write", "ff01", b"\x22\x00", False), client.events)

    async def test_host_limit_fragments_logical_blocks_without_changing_prn_window(self):
        firmware = bytes(index % 251 for index in range(3964))
        first_ack_fragment = firmware[3880:3904]
        final_fragment = firmware[3948:3964]
        threshold_checksum = 21464
        final_checksum = 31574
        upgrade = gatt_ota.pixart_ota.build_upgrade(
            len(firmware), final_checksum, gatt_ota.OTA_VERSION
        )
        client = FakeGattClient(
            reads=[init_response(max_object_size=4096, mtu_size=244, prn_threshold=16)],
            mtu_size=50,
            notify_after_write={
                bytes.fromhex("25 00000000 00100000"): [bytes.fromhex("25 000000")],
                first_ack_fragment: [
                    b"\x17" + threshold_checksum.to_bytes(2, "little")
                ],
                final_fragment: [
                    b"\x17" + final_checksum.to_bytes(2, "little")
                ],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )
        engine = gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            chunk_pacing_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.1,
        )

        await engine.flash(authorize(firmware))

        self.assertEqual(engine.last_state.mtu_size, 244)
        self.assertEqual(engine.host_wnr_limit, 47)
        self.assertEqual(engine.physical_fragment_size, 44)
        payload_writes = [
            event[2]
            for event in client.events
            if event[0] == "write"
            and event[3] is False
            and event[2] != b"\x22\x00"
        ]
        self.assertEqual(b"".join(payload_writes), firmware)
        self.assertTrue(all(len(fragment) <= 44 for fragment in payload_writes))
        self.assertTrue(all(len(fragment) % 4 == 0 for fragment in payload_writes))
        self.assertEqual(
            [len(fragment) for fragment in payload_writes[:6]],
            [44, 44, 44, 44, 44, 24],
        )
        self.assertEqual(
            [len(fragment) for fragment in payload_writes[-2:]], [44, 16]
        )
        first_ack_write_index = payload_writes.index(first_ack_fragment)
        self.assertEqual(
            sum(map(len, payload_writes[: first_ack_write_index + 1])),
            3904,
        )
        self.assertIn(("write", "ff01", b"\x22\x00", False), client.events)

    async def test_fragmented_transfer_uses_matching_object_end_checksum_ack(self):
        firmware = (
            b"\xff" * 61
            + b"\x99"
            + b"\x00" * (3313 - 62)
            + b"\xff" * 243
            + b"\x0b"
            + b"\x00" * (4096 - 3313 - 244)
        )
        upgrade = gatt_ota.pixart_ota.build_upgrade(
            len(firmware), 0x2F74, gatt_ota.OTA_VERSION
        )
        client = ObjectEndChecksumGattClient(
            final_ack=bytes.fromhex("17 00 74 2f"),
            reads=[init_response(max_object_size=4096, mtu_size=244, prn_threshold=16)],
            mtu_size=50,
            notify_after_write={
                bytes.fromhex("25 00000000 00100000"): [bytes.fromhex("25 000000")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )

        await gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            chunk_pacing_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.05,
        ).flash(authorize(firmware))

        payload_writes = [
            event[2]
            for event in client.events
            if event[0] == "write"
            and event[3] is False
            and event[2] != b"\x22\x00"
        ]
        self.assertEqual(b"".join(payload_writes), firmware)
        self.assertIn(("write", "ff01", upgrade, True), client.events)
        self.assertIn(("write", "ff01", b"\x22\x00", False), client.events)

    async def test_fragmented_object_end_timeout_reports_last_checksum_mismatch(self):
        firmware = (
            b"\xff" * 61
            + b"\x99"
            + b"\x00" * (3313 - 62)
            + b"\xff" * 243
            + b"\x0b"
            + b"\x00" * (4096 - 3313 - 244)
        )
        upgrade = gatt_ota.pixart_ota.build_upgrade(
            len(firmware), 0x2F74, gatt_ota.OTA_VERSION
        )
        client = ObjectEndChecksumGattClient(
            final_ack=None,
            reads=[init_response(max_object_size=4096, mtu_size=244, prn_threshold=16)],
            mtu_size=50,
            notify_after_write={
                bytes.fromhex("25 00000000 00100000"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaisesRegex(
            gatt_ota.GattTimeoutError,
            r"expected=0x2F74, received=0x3D5C, raw=17 00 5c 3d",
        ):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                chunk_pacing_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(firmware))

        payload_writes = [
            event[2]
            for event in client.events
            if event[0] == "write"
            and event[3] is False
            and event[2] != b"\x22\x00"
        ]
        self.assertEqual(b"".join(payload_writes), firmware)
        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(upgrade, writes)
        self.assertNotIn(b"\x22\x00", writes)

    async def test_unfragmented_transfer_still_waits_at_intermediate_prn_boundary(self):
        firmware = bytes(range(1, 13))
        client = FakeGattClient(
            reads=[init_response(max_object_size=12, mtu_size=4, prn_threshold=2)],
            mtu_size=7,
            notify_after_write={
                bytes.fromhex("25 00000000 0c000000"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaises(gatt_ota.GattTimeoutError):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                chunk_pacing_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(firmware))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertIn(bytes(range(5, 9)), writes)
        self.assertNotIn(bytes(range(9, 13)), writes)

    async def test_invalid_or_missing_host_mtu_fails_closed_before_object(self):
        for host_mtu in (None, True, 3, "247"):
            with self.subTest(host_mtu=host_mtu):
                client = FakeGattClient(reads=[init_response()])
                if host_mtu is None:
                    del client.mtu_size
                else:
                    client.mtu_size = host_mtu
                engine = gatt_ota.GattOtaEngine(
                    client,
                    settle_seconds=0,
                    operation_timeout=0.1,
                    ack_timeout=0.1,
                )

                with self.assertRaisesRegex(
                    gatt_ota.GattProtocolError, "host WNR limit"
                ):
                    await engine.flash(authorize(b"\x01\x02"))

                writes = [event[2] for event in client.events if event[0] == "write"]
                self.assertNotIn(bytes.fromhex("25 00000000 04000000"), writes)
                self.assertNotIn(b"\x01\x02", writes)
                self.assertNotIn(b"\x22\x00", writes)

    async def test_payload_ack_timeout_reports_chunk_and_never_finalizes(self):
        client = CoreBluetoothGattClient(
            ready=[False, True],
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaisesRegex(
            gatt_ota.GattTimeoutError,
            r"object 0 logical payload 0 length 2 expected ACK 0x17.*"
            r"offset=0, checksum=0x0000, max_object_size=4, mtu_size=4, "
            r"prn_threshold=1.*host WNR limit=244.*"
            r"physical fragment size=4.*"
            r"physical fragment pacing=0.010s.*"
            r"WNR readiness false observations=1",
        ):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(bytes.fromhex("18 02000000 0300 312e302e31"), writes)
        self.assertNotIn(b"\x22\x00", writes)

    async def test_payload_write_timeout_reports_state_and_never_finalizes(self):
        client = SlowPayloadGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaisesRegex(
            gatt_ota.GattTimeoutError,
            r"object 0 logical payload 0 length 2 payload write.*"
            r"offset=0, checksum=0x0000, max_object_size=4, mtu_size=4, "
            r"prn_threshold=1.*host WNR limit=244.*"
            r"physical fragment size=4",
        ):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                chunk_pacing_seconds=0,
                operation_timeout=0.01,
                ack_timeout=0.1,
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(bytes.fromhex("18 02000000 0300 312e302e31"), writes)
        self.assertNotIn(b"\x22\x00", writes)

    async def test_cleanup_failure_after_success_is_reported(self):
        firmware = b"\x01\x02"
        object_create = bytes.fromhex("25 00000000 04000000")
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = FailingStopGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                object_create: [bytes.fromhex("25 000000")],
                firmware: [bytes.fromhex("17 0300")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )

        with self.assertRaisesRegex(gatt_ota.GattOtaError, "stop notify"):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(firmware))

    async def test_cleanup_failure_does_not_replace_primary_failure(self):
        client = FailingStopGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("18 0000")]
            },
        )

        with self.assertRaisesRegex(gatt_ota.GattProtocolError, "unexpected ACK"):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

    async def test_successful_reset_write_may_disconnect_immediately(self):
        firmware = b"\x01\x02"
        object_create = bytes.fromhex("25 00000000 04000000")
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = DisconnectOnResetGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                object_create: [bytes.fromhex("25 000000")],
                firmware: [bytes.fromhex("17 0300")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )
        client.backend_id = "core_bluetooth"
        client._backend = SimpleNamespace(
            _peripheral=CoreBluetoothPeripheral([True, True])
        )

        await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        ).flash(authorize(firmware))

        self.assertFalse(client.is_connected)
        self.assertNotIn(("stop-notify", "ff01"), client.events)

    async def test_reset_write_exception_is_not_treated_as_success(self):
        firmware = b"\x01\x02"
        object_create = bytes.fromhex("25 00000000 04000000")
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = RaiseOnResetGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                object_create: [bytes.fromhex("25 000000")],
                firmware: [bytes.fromhex("17 0300")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )

        with self.assertRaises(gatt_ota.GattOtaError):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(firmware))

    async def test_stale_object_ack_is_drained_before_object_create(self):
        object_create = bytes.fromhex("25 00000000 04000000")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_burst_after_write={
                bytes.fromhex("27 02000000 00"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaises(gatt_ota.GattTimeoutError):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertIn(object_create, writes)
        self.assertNotIn(b"\x01\x02", writes)

    async def test_stale_prn_ack_is_drained_before_payload_window(self):
        object_create = bytes.fromhex("25 00000000 04000000")
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_burst_after_write={
                object_create: [bytes.fromhex("25 000000"), bytes.fromhex("17 0300")]
            },
        )

        with self.assertRaises(gatt_ota.GattTimeoutError):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertIn(b"\x01\x02", writes)
        self.assertNotIn(upgrade, writes)

    async def test_stale_upgrade_ack_is_drained_before_upgrade_write_and_wait_is_cancelable(
        self,
    ):
        object_create = bytes.fromhex("25 00000000 04000000")
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={object_create: [bytes.fromhex("25 000000")]},
            notify_burst_after_write={
                b"\x01\x02": [
                    bytes.fromhex("17 0300"),
                    bytes.fromhex("18 0000"),
                ]
            },
        )

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                gatt_ota.GattOtaEngine(
                    client,
                    settle_seconds=0,
                    operation_timeout=0.1,
                    ack_timeout=0.01,
                ).flash(authorize(b"\x01\x02")),
                timeout=0.03,
            )

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertIn(upgrade, writes)
        self.assertNotIn(b"\x22\x00", writes)

    async def test_stale_prn_acks_during_next_object_create_are_ignored(self):
        firmware = b"\x01\x02\x03\x04\x05\x06"
        first_object_create = bytes.fromhex("25 00000000 04000000")
        second_object_create = bytes.fromhex("25 04000000 04000000")
        upgrade = bytes.fromhex("18 06000000 1500 312e302e31")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=2)],
            notify_after_write={
                first_object_create: [bytes.fromhex("25 000000")],
                firmware[:4]: [bytes.fromhex("17 0a00")],
                b"\x05\x06": [bytes.fromhex("17 1500")],
                upgrade: [bytes.fromhex("18 0000")],
            },
            notify_burst_after_write={
                second_object_create: [
                    bytes.fromhex("17 0a00"),
                    bytes.fromhex("17 0a00"),
                    bytes.fromhex("25 000000"),
                ]
            },
        )

        await gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            chunk_pacing_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.1,
        ).flash(authorize(firmware))

        self.assertIn(("write", "ff01", b"\x22\x00", False), client.events)

    async def test_stale_prn_acks_after_upgrade_write_are_ignored(self):
        firmware = b"\x01\x02"
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [
                    bytes.fromhex("25 000000")
                ],
                firmware: [bytes.fromhex("17 0300")],
            },
            notify_burst_after_write={
                upgrade: [
                    bytes.fromhex("17 0300"),
                    bytes.fromhex("17 0300"),
                    bytes.fromhex("18 0000"),
                ]
            },
        )

        await gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.1,
        ).flash(authorize(firmware))

        self.assertIn(("write", "ff01", b"\x22\x00", False), client.events)

    async def test_upgrade_wait_ignores_ack_timeout_until_delayed_success_ack(self):
        firmware = b"\x01\x02"
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = DelayedUpgradeAckGattClient(
            upgrade=upgrade,
            upgrade_ack=bytes.fromhex("18 0000"),
            delay=0.02,
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [
                    bytes.fromhex("25 000000")
                ],
                firmware: [bytes.fromhex("17 0300")],
            },
        )

        await gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            chunk_pacing_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.01,
        ).flash(authorize(firmware))

        self.assertIn(("write", "ff01", b"\x22\x00", False), client.events)

    async def test_upgrade_wait_with_only_stale_prn_acks_is_cancelable_without_reset(self):
        firmware = b"\x01\x02"
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [
                    bytes.fromhex("25 000000")
                ],
                firmware: [bytes.fromhex("17 0300")],
            },
            notify_burst_after_write={
                upgrade: [bytes.fromhex("17 0300"), bytes.fromhex("17 0300")]
            },
        )

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                gatt_ota.GattOtaEngine(
                    client,
                    settle_seconds=0,
                    operation_timeout=0.1,
                    ack_timeout=0.01,
                ).flash(authorize(firmware)),
                timeout=0.03,
            )

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(b"\x22\x00", writes)

    async def test_only_stale_prn_acks_while_waiting_for_object_times_out(self):
        firmware = b"\x01\x02\x03\x04\x05\x06"
        first_object_create = bytes.fromhex("25 00000000 04000000")
        second_object_create = bytes.fromhex("25 04000000 04000000")
        upgrade = bytes.fromhex("18 06000000 1500 312e302e31")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=2)],
            notify_after_write={
                first_object_create: [bytes.fromhex("25 000000")],
                firmware[:4]: [bytes.fromhex("17 0a00")],
            },
            notify_burst_after_write={
                second_object_create: [
                    bytes.fromhex("17 0a00"),
                    bytes.fromhex("17 0a00"),
                ]
            },
        )

        with self.assertRaisesRegex(gatt_ota.GattTimeoutError, "ACK 0x25"):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(firmware))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(b"\x05\x06", writes)
        self.assertNotIn(upgrade, writes)
        self.assertNotIn(b"\x22\x00", writes)

    async def test_engine_rejects_second_use_before_ble(self):
        client = small_transfer_client()
        engine = gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        )
        approved = authorize(b"\x01\x02")
        await engine.flash(approved)
        event_count = len(client.events)

        with self.assertRaisesRegex(gatt_ota.GattProtocolError, "single-use"):
            await engine.flash(approved)

        self.assertEqual(len(client.events), event_count)

    async def test_engine_rejects_concurrent_use_before_second_ble_operation(self):
        client = FirstStartBlockingGattClient(reads=[])
        engine = gatt_ota.GattOtaEngine(client, operation_timeout=1)
        approved = authorize(b"\x01")
        first = asyncio.create_task(engine.flash(approved))
        await asyncio.wait_for(client.first_start.wait(), timeout=0.1)
        event_count = len(client.events)

        with self.assertRaisesRegex(gatt_ota.GattProtocolError, "single-use"):
            await engine.flash(approved)

        self.assertEqual(len(client.events), event_count)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first

    async def test_raw_or_wrong_mode_firmware_is_rejected_before_ble(self):
        for firmware in (b"raw", authorize(b"global", recovery=True)):
            with self.subTest(firmware=firmware):
                client = FakeGattClient(reads=[])

                with self.assertRaises(gatt_ota.GattProtocolError):
                    await gatt_ota.GattOtaEngine(client).flash(firmware)

                self.assertEqual(client.events, [])

    async def test_cancellation_during_cleanup_is_not_swallowed(self):
        firmware = b"\x01\x02"
        upgrade = bytes.fromhex("18 02000000 0300 312e302e31")
        client = StopBlockingGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")],
                firmware: [bytes.fromhex("17 0300")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )
        task = asyncio.create_task(
            gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                operation_timeout=1,
                ack_timeout=0.1,
            ).flash(authorize(firmware))
        )
        await asyncio.wait_for(client.stop_started.wait(), timeout=0.1)

        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_len4_checksum_ack_uses_bytes_two_and_three(self):
        client = small_transfer_client(checksum_ack=bytes.fromhex("17 ff 0300"))

        await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        ).flash(authorize(b"\x01\x02"))

        self.assertIn(("write", "ff01", b"\x22\x00", False), client.events)

    async def test_unexpected_object_ack_stops_without_reset(self):
        client = small_transfer_client(object_ack=bytes.fromhex("18 0000"))

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError,
            r"unexpected ACK during ACK 0x25; raw=18 00 00",
        ):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

        self.assertNotIn(b"\x22\x00", [e[2] for e in client.events if e[0] == "write"])
        self.assertEqual(client.events[-1], ("stop-notify", "ff01"))

    async def test_malformed_stale_prn_during_object_ack_is_not_ignored(self):
        client = small_transfer_client(object_ack=bytes.fromhex("17"))

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError,
            r"unexpected ACK during ACK 0x25; raw=17",
        ):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertFalse(any(payload.startswith(b"\x18") for payload in writes))
        self.assertNotIn(b"\x22\x00", writes)

    async def test_object_ack_payload_is_opaque_after_opcode(self):
        client = small_transfer_client(object_ack=bytes.fromhex("25 deadbe"))

        await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        ).flash(authorize(b"\x01\x02"))

        self.assertIn(("write", "ff01", b"\x22\x00", False), client.events)

    async def test_malformed_or_mismatched_checksum_ack_stops_without_reset(self):
        for frame in (bytes.fromhex("17 03"), bytes.fromhex("17 0400")):
            with self.subTest(frame=frame.hex()):
                client = small_transfer_client(checksum_ack=frame)

                with self.assertRaises(gatt_ota.GattProtocolError):
                    await gatt_ota.GattOtaEngine(
                        client,
                        settle_seconds=0,
                        operation_timeout=0.1,
                        ack_timeout=0.1,
                    ).flash(authorize(b"\x01\x02"))

                self.assertNotIn(
                    b"\x22\x00",
                    [e[2] for e in client.events if e[0] == "write"],
                )

    async def test_mismatched_checksum_reports_expected_received_and_raw_ack(self):
        client = small_transfer_client(checksum_ack=bytes.fromhex("17 0400"))

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError,
            r"expected=0x0003, received=0x0004, raw=17 04 00",
        ):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.1,
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertFalse(any(payload.startswith(b"\x18") for payload in writes))
        self.assertNotIn(b"\x22\x00", writes)

    async def test_early_checksum_collision_ack_cannot_satisfy_prn_boundary(self):
        firmware = b"\x01\x02\x00\x00"
        upgrade = bytes.fromhex("18 04000000 0300 312e302e31")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=2)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")],
                b"\x01\x02": [bytes.fromhex("17 0300")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )

        with self.assertRaises(gatt_ota.GattTimeoutError):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(firmware))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(upgrade, writes)
        self.assertNotIn(b"\x22\x00", writes)

    async def test_prn_ack_during_final_physical_fragment_is_accepted(self):
        firmware = b"\x01\x02\x03\x04"
        final_fragment = firmware
        upgrade = bytes.fromhex("18 04000000 0a00 312e302e31")
        client = AckBeforeWriteReturnsGattClient(
            final_payload=final_fragment,
            ack=bytes.fromhex("17 0a00"),
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            mtu_size=7,
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )

        await gatt_ota.GattOtaEngine(
            client,
            settle_seconds=0,
            chunk_pacing_seconds=0,
            operation_timeout=0.1,
            ack_timeout=0.01,
        ).flash(authorize(firmware))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertIn(upgrade, writes)
        self.assertIn(b"\x22\x00", writes)

    async def test_prn_ack_during_wnr_readiness_is_not_current_payload_ack(self):
        client = CoreBluetoothGattClient(
            ready=[],
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")]
            },
        )

        def notify_before_ready():
            client._callback("ff01", bytes.fromhex("17 0300"))
            return False

        client.peripheral.ready.extend((notify_before_ready, True))

        with self.assertRaises(gatt_ota.GattTimeoutError):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                chunk_pacing_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(
            bytes.fromhex("18 02000000 0300 312e302e31"), writes
        )
        self.assertNotIn(b"\x22\x00", writes)

    async def test_queued_stale_prn_ack_before_native_write_is_not_current(self):
        client = CoreBluetoothGattClient(
            ready=[True],
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [bytes.fromhex("25 000000")]
            },
        )

        with self.assertRaises(gatt_ota.GattTimeoutError):
            await StaleAckBeforePayloadTaskEngine(
                client,
                settle_seconds=0,
                chunk_pacing_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(
            bytes.fromhex("18 02000000 0300 312e302e31"), writes
        )
        self.assertNotIn(b"\x22\x00", writes)

    async def test_upgrade_ack_failure_or_malformed_frame_stops_without_reset(self):
        for frame in (bytes.fromhex("18 01 0000"), bytes.fromhex("18 00")):
            with self.subTest(frame=frame.hex()):
                client = small_transfer_client(upgrade_ack=frame)

                with self.assertRaises(gatt_ota.GattProtocolError):
                    await gatt_ota.GattOtaEngine(
                        client,
                        settle_seconds=0,
                        operation_timeout=0.1,
                        ack_timeout=0.1,
                    ).flash(authorize(b"\x01\x02"))

                self.assertNotIn(
                    b"\x22\x00",
                    [e[2] for e in client.events if e[0] == "write"],
                )

    async def test_upgrade_rejection_reports_raw_status_and_post_rejection_state(self):
        client = small_transfer_client(upgrade_ack=bytes.fromhex("18 ff a5 5a"))
        client._reads.append(
            init_response(
                offset=1,
                checksum=0x1234,
                max_object_size=4096,
                mtu_size=244,
                prn_threshold=16,
            )
        )
        engine = gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        )

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError,
            r"upgrade ACK reports failure: status=0xFF, raw=18 ff a5 5a; "
            r"post-rejection state: offset=1, checksum=0x1234, "
            r"max_object_size=4096, mtu_size=244, prn_threshold=16",
        ):
            await engine.flash(authorize(b"\x01\x02"))

        self.assertEqual(
            (engine.last_state.offset, engine.last_state.checksum), (1, 0x1234)
        )
        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertEqual(writes.count(bytes.fromhex("27 02000000 00")), 2)
        self.assertNotIn(b"\x22\x00", writes)

    async def test_upgrade_rejection_is_preserved_when_state_query_fails(self):
        client = small_transfer_client(upgrade_ack=bytes.fromhex("18 02 de ad"))
        client._reads.append(b"invalid")
        engine = gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        )

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError,
            r"upgrade ACK reports failure: status=0x02, raw=18 02 de ad; "
            r"post-rejection state query failed: invalid init-new response",
        ):
            await engine.flash(authorize(b"\x01\x02"))

        self.assertEqual((engine.last_state.offset, engine.last_state.checksum), (0, 0))
        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertEqual(writes.count(bytes.fromhex("27 02000000 00")), 2)
        self.assertNotIn(b"\x22\x00", writes)

    async def test_unexpected_object_ack_while_waiting_for_upgrade_reports_raw(self):
        client = small_transfer_client(upgrade_ack=bytes.fromhex("25 deadbe"))

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError,
            r"unexpected ACK during ACK 0x18; raw=25 de ad be",
        ):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

        self.assertNotIn(b"\x22\x00", [e[2] for e in client.events if e[0] == "write"])

    async def test_missing_ack_times_out_and_stops_notifications(self):
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=4, prn_threshold=1)]
        )

        with self.assertRaises(gatt_ota.GattTimeoutError):
            await gatt_ota.GattOtaEngine(
                client,
                settle_seconds=0,
                operation_timeout=0.1,
                ack_timeout=0.01,
            ).flash(authorize(b"\x01\x02"))

        self.assertEqual(client.events[-1], ("stop-notify", "ff01"))
        self.assertNotIn(b"\x22\x00", [e[2] for e in client.events if e[0] == "write"])

    async def test_start_write_and_read_operations_have_finite_timeouts(self):
        for operation in ("start-notify", "write", "read"):
            with self.subTest(operation=operation):
                client = SlowGattClient(
                    slow_operation=operation,
                    reads=[init_response()],
                )

                with self.assertRaises(gatt_ota.GattTimeoutError):
                    await gatt_ota.GattOtaEngine(
                        client,
                        settle_seconds=0,
                        operation_timeout=0.01,
                        ack_timeout=0.01,
                    ).flash(authorize(b"\x01\x02"))

                self.assertNotIn(
                    b"\x22\x00",
                    [e[2] for e in client.events if e[0] == "write"],
                )

    async def test_disconnect_after_write_is_reported_without_reset(self):
        client = DisconnectingGattClient(reads=[init_response()])

        with self.assertRaises(gatt_ota.GattDisconnectedError):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertNotIn(b"\x22\x00", writes)

    async def test_already_disconnected_client_does_not_leak_unawaited_coroutine(self):
        client = FakeGattClient(reads=[])
        client.is_connected = False

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(gatt_ota.GattDisconnectedError):
                await gatt_ota.GattOtaEngine(client).flash(authorize(b"\x01"))
            gc.collect()

        self.assertFalse(
            [warning for warning in caught if "never awaited" in str(warning.message)]
        )

    async def test_malformed_init_response_fails_before_object_write(self):
        client = FakeGattClient(reads=[b"\x00"])

        with self.assertRaises(gatt_ota.GattProtocolError):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertEqual(writes, [b"\x28\x00", bytes.fromhex("27 02000000 00")])

    async def test_non_positive_timeouts_are_rejected(self):
        client = FakeGattClient(reads=[])

        for options in (
            {"operation_timeout": 0},
            {"operation_timeout": -1},
            {"ack_timeout": 0},
            {"ack_timeout": -1},
            {"settle_seconds": -1},
            {"chunk_pacing_seconds": -1},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                gatt_ota.GattOtaEngine(client, **options)

    async def test_empty_firmware_is_rejected_before_any_ble_operation(self):
        client = FakeGattClient(reads=[])

        with self.assertRaises(gatt_ota.GattProtocolError):
            await gatt_ota.GattOtaEngine(client).flash(b"")

        self.assertEqual(client.events, [])

    async def test_normal_transfer_uses_exact_endpoints_modes_and_order(self):
        firmware = bytes(range(1, 7))
        client = FakeGattClient(
            reads=[init_response()],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [b"\x25"],
                firmware[:4]: [bytes.fromhex("17 0a 00")],
                bytes.fromhex("25 04000000 04000000"): [b"\x25"],
                b"\x05\x06": [bytes.fromhex("17 15 00")],
                bytes.fromhex("18 06000000 1500 312e302e31"): [
                    bytes.fromhex("18 00 00")
                ],
            },
        )

        state = await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        ).flash(authorize(firmware))

        self.assertEqual(state.offset, 0)
        event_shapes = [
            (event[0], *event[1:3]) if event[0] == "write" else event[:2]
            for event in client.events
        ]
        self.assertEqual(
            event_shapes,
            [
                ("start-notify", "ff01"),
                ("write", "ff02", b"\x28\x00"),
                ("write", "ff01", bytes.fromhex("27 06000000 00")),
                ("read", "ff01"),
                ("write", "ff01", bytes.fromhex("25 00000000 04000000")),
                ("write", "ff01", firmware[:4]),
                ("write", "ff01", bytes.fromhex("25 04000000 04000000")),
                ("write", "ff01", b"\x05\x06"),
                (
                    "write",
                    "ff01",
                    bytes.fromhex("18 06000000 1500 312e302e31"),
                ),
                ("write", "ff01", b"\x22\x00"),
                ("stop-notify", "ff01"),
            ],
        )
        writes = [event for event in client.events if event[0] == "write"]
        self.assertEqual(
            [event[3] for event in writes],
            [True, True, True, False, True, False, True, False],
        )
        options = client.events[0][2]
        discriminator = options["cb"]["notification_discriminator"]
        self.assertTrue(discriminator(b"\x17\x00\x00"))
        self.assertFalse(discriminator(b"\x99"))


class RecoveryTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_state_only_writes_init_new_and_reads_state(self):
        client = FakeGattClient(
            reads=[
                init_response(
                    offset=3,
                    checksum=0x1234,
                    max_object_size=4096,
                    mtu_size=244,
                    prn_threshold=16,
                )
            ]
        )

        state = await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1
        ).inspect_state(123_916)

        self.assertEqual((state.offset, state.checksum), (3, 0x1234))
        self.assertEqual(
            client.events,
            [
                ("write", "ff01", bytes.fromhex("27 0ce40100 00"), True),
                ("read", "ff01"),
            ],
        )

    async def test_flash_reports_state_when_retransmit_does_not_clear_resume(self):
        client = FakeGattClient(reads=[init_response(offset=1, checksum=10)])

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError,
            r"retransmit did not clear resume state; offset=1, checksum=0x000A",
        ):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(bytes(range(1, 7))))

    async def test_raw_or_wrong_mode_firmware_is_rejected_before_ble(self):
        for firmware in (b"raw", authorize(b"jp", recovery=False)):
            with self.subTest(firmware=firmware):
                client = FakeGattClient(reads=[])

                with self.assertRaises(gatt_ota.GattProtocolError):
                    await gatt_ota.GattOtaEngine(client).recover(firmware)

                self.assertEqual(client.events, [])

    async def test_recovery_retransmits_before_init_and_transfers_full_firmware(self):
        firmware = bytes(range(1, 7))
        upgrade = bytes.fromhex("18 06000000 1500 312e302e31")
        client = FakeGattClient(
            reads=[init_response(offset=0, checksum=0)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [b"\x25"],
                firmware[:4]: [bytes.fromhex("17 0a 00")],
                bytes.fromhex("25 04000000 04000000"): [b"\x25"],
                b"\x05\x06": [bytes.fromhex("17 15 00")],
                upgrade: [bytes.fromhex("18 00 00")],
            },
        )

        state = await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        ).recover(authorize(firmware, recovery=True))

        self.assertEqual((state.offset, state.checksum), (0, 0))
        writes = [event for event in client.events if event[0] == "write"]
        self.assertEqual(
            [(event[1], event[2], event[3]) for event in writes],
            [
                ("ff02", b"\x28\x00", True),
                ("ff01", bytes.fromhex("27 06000000 00"), True),
                ("ff01", bytes.fromhex("25 00000000 04000000"), True),
                ("ff01", firmware[:4], False),
                ("ff01", bytes.fromhex("25 04000000 04000000"), True),
                ("ff01", b"\x05\x06", False),
                ("ff01", upgrade, True),
                ("ff01", b"\x22\x00", False),
            ],
        )

    async def test_recovery_uses_single_post_retransmit_state_query(self):
        firmware = bytes(range(1, 7))
        upgrade = bytes.fromhex("18 06000000 1500 312e302e31")
        client = FakeGattClient(
            reads=[init_response(offset=0, checksum=0)],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [b"\x25"],
                firmware[:4]: [bytes.fromhex("17 0a 00")],
                bytes.fromhex("25 04000000 04000000"): [b"\x25"],
                b"\x05\x06": [bytes.fromhex("17 15 00")],
                upgrade: [bytes.fromhex("18 00 00")],
            },
        )

        state = await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        ).recover(authorize(firmware, recovery=True))

        self.assertEqual((state.offset, state.checksum), (0, 0))
        writes = [event for event in client.events if event[0] == "write"]
        self.assertEqual(
            [(event[1], event[2], event[3]) for event in writes[:3]],
            [
                ("ff02", b"\x28\x00", True),
                ("ff01", bytes.fromhex("27 06000000 00"), True),
                ("ff01", bytes.fromhex("25 00000000 04000000"), True),
            ],
        )

    async def test_recovery_stops_when_post_retransmit_state_is_not_zero(self):
        firmware = bytes(range(1, 7))
        client = FakeGattClient(
            reads=[init_response(offset=1, checksum=10)]
        )
        engine = gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        )

        with self.assertRaisesRegex(
            gatt_ota.GattProtocolError,
            r"retransmit did not clear resume state; "
            r"offset=1, checksum=0x000A",
        ):
            await engine.recover(authorize(firmware, recovery=True))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertEqual(
            writes,
            [
                b"\x28\x00",
                bytes.fromhex("27 06000000 00"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
