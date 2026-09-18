import asyncio
import gc
import struct
import unittest
import warnings
from collections import defaultdict, deque
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
    mtu_size: int = 2,
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
    ) -> None:
        self.is_connected = True
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
        for frame in self._notification_bursts.pop(payload, ()):
            self._callback(characteristic, frame)
        if self._notifications[payload]:
            self._callback(characteristic, self._notifications[payload].popleft())

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


def small_transfer_client(
    *,
    object_ack: bytes = bytes.fromhex("25 000000"),
    checksum_ack: bytes = bytes.fromhex("17 0300"),
    upgrade_ack: bytes = bytes.fromhex("18 0000"),
) -> FakeGattClient:
    firmware = b"\x01\x02"
    upgrade = bytes.fromhex("18 02000000 0300 312e302e310000000000")
    return FakeGattClient(
        reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
        notify_after_write={
            bytes.fromhex("25 00000000 04000000"): [object_ack],
            firmware: [checksum_ack],
            upgrade: [upgrade_ack],
        },
    )


class NormalTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_failure_after_success_is_reported(self):
        firmware = b"\x01\x02"
        object_create = bytes.fromhex("25 00000000 04000000")
        upgrade = bytes.fromhex("18 02000000 0300 312e302e310000000000")
        client = FailingStopGattClient(
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
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
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
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
        upgrade = bytes.fromhex("18 02000000 0300 312e302e310000000000")
        client = DisconnectOnResetGattClient(
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
            notify_after_write={
                object_create: [bytes.fromhex("25 000000")],
                firmware: [bytes.fromhex("17 0300")],
                upgrade: [bytes.fromhex("18 0000")],
            },
        )

        await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        ).flash(authorize(firmware))

        self.assertFalse(client.is_connected)
        self.assertNotIn(("stop-notify", "ff01"), client.events)

    async def test_reset_write_exception_is_not_treated_as_success(self):
        firmware = b"\x01\x02"
        object_create = bytes.fromhex("25 00000000 04000000")
        upgrade = bytes.fromhex("18 02000000 0300 312e302e310000000000")
        client = RaiseOnResetGattClient(
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
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
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
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
        upgrade = bytes.fromhex("18 02000000 0300 312e302e310000000000")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
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

    async def test_stale_upgrade_ack_is_drained_before_upgrade_write(self):
        object_create = bytes.fromhex("25 00000000 04000000")
        upgrade = bytes.fromhex("18 02000000 0300 312e302e310000000000")
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
            notify_after_write={object_create: [bytes.fromhex("25 000000")]},
            notify_burst_after_write={
                b"\x01\x02": [
                    bytes.fromhex("17 0300"),
                    bytes.fromhex("18 0000"),
                ]
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
        self.assertIn(upgrade, writes)
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
        upgrade = bytes.fromhex("18 02000000 0300 312e302e310000000000")
        client = StopBlockingGattClient(
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)],
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

        with self.assertRaisesRegex(gatt_ota.GattProtocolError, "unexpected ACK"):
            await gatt_ota.GattOtaEngine(
                client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
            ).flash(authorize(b"\x01\x02"))

        self.assertNotIn(b"\x22\x00", [e[2] for e in client.events if e[0] == "write"])
        self.assertEqual(client.events[-1], ("stop-notify", "ff01"))

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

    async def test_missing_ack_times_out_and_stops_notifications(self):
        client = FakeGattClient(
            reads=[init_response(max_object_size=4, mtu_size=2, prn_threshold=1)]
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
                b"\x03\x04": [bytes.fromhex("17 0a 00")],
                bytes.fromhex("25 04000000 04000000"): [b"\x25"],
                b"\x05\x06": [bytes.fromhex("17 15 00")],
                bytes.fromhex("18 06000000 1500 312e302e310000000000"): [
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
                ("write", "ff01", b"\x01\x02"),
                ("write", "ff01", b"\x03\x04"),
                ("write", "ff01", bytes.fromhex("25 04000000 04000000")),
                ("write", "ff01", b"\x05\x06"),
                (
                    "write",
                    "ff01",
                    bytes.fromhex("18 06000000 1500 312e302e310000000000"),
                ),
                ("write", "ff01", b"\x22\x00"),
                ("stop-notify", "ff01"),
            ],
        )
        writes = [event for event in client.events if event[0] == "write"]
        self.assertEqual(
            [event[3] for event in writes],
            [True, True, True, False, False, True, False, True, False],
        )
        options = client.events[0][2]
        discriminator = options["cb"]["notification_discriminator"]
        self.assertTrue(discriminator(b"\x17\x00\x00"))
        self.assertFalse(discriminator(b"\x99"))


class RecoveryTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_or_wrong_mode_firmware_is_rejected_before_ble(self):
        for firmware in (b"raw", authorize(b"jp", recovery=False)):
            with self.subTest(firmware=firmware):
                client = FakeGattClient(reads=[])

                with self.assertRaises(gatt_ota.GattProtocolError):
                    await gatt_ota.GattOtaEngine(client).recover(firmware)

                self.assertEqual(client.events, [])

    async def test_valid_resume_continues_without_retransmit(self):
        firmware = bytes(range(1, 7))
        upgrade = bytes.fromhex("18 06000000 1500 312e302e310000000000")
        client = FakeGattClient(
            reads=[init_response(offset=1, checksum=10)],
            notify_after_write={
                bytes.fromhex("25 04000000 04000000"): [b"\x25"],
                b"\x05\x06": [bytes.fromhex("17 15 00")],
                upgrade: [bytes.fromhex("18 00 00")],
            },
        )

        state = await gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        ).recover(authorize(firmware, recovery=True))

        self.assertEqual((state.offset, state.checksum), (1, 10))
        writes = [event for event in client.events if event[0] == "write"]
        self.assertEqual(
            [(event[1], event[2], event[3]) for event in writes],
            [
                ("ff01", bytes.fromhex("27 06000000 00"), True),
                ("ff01", bytes.fromhex("25 04000000 04000000"), True),
                ("ff01", b"\x05\x06", False),
                ("ff01", upgrade, True),
                ("ff01", b"\x22\x00", False),
            ],
        )

    async def test_invalid_resume_falls_back_through_retransmit_and_zero_state(self):
        firmware = bytes(range(1, 7))
        upgrade = bytes.fromhex("18 06000000 1500 312e302e310000000000")
        client = FakeGattClient(
            reads=[
                init_response(offset=1, checksum=99),
                init_response(offset=0, checksum=0),
            ],
            notify_after_write={
                bytes.fromhex("25 00000000 04000000"): [b"\x25"],
                b"\x03\x04": [bytes.fromhex("17 0a 00")],
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
                ("ff01", bytes.fromhex("27 06000000 00"), True),
                ("ff02", b"\x28\x00", True),
                ("ff01", bytes.fromhex("27 06000000 00"), True),
            ],
        )

    async def test_recovery_stops_when_fallback_state_is_not_zero(self):
        firmware = bytes(range(1, 7))
        client = FakeGattClient(
            reads=[
                init_response(offset=9, checksum=0),
                init_response(offset=1, checksum=10),
            ]
        )
        engine = gatt_ota.GattOtaEngine(
            client, settle_seconds=0, operation_timeout=0.1, ack_timeout=0.1
        )

        with self.assertRaisesRegex(gatt_ota.GattProtocolError, "zero"):
            await engine.recover(authorize(firmware, recovery=True))

        writes = [event[2] for event in client.events if event[0] == "write"]
        self.assertEqual(
            writes,
            [
                bytes.fromhex("27 06000000 00"),
                b"\x28\x00",
                bytes.fromhex("27 06000000 00"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
