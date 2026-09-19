"""Fail-closed PixArt OTA execution over the confirmed GATT endpoints."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Never

if __package__:
    from . import firmware_image, keymap_config, phase3_build_patch, pixart_ota
else:
    import firmware_image
    import keymap_config
    import phase3_build_patch
    import pixart_ota


CONTROL_CHARACTERISTIC = "ff01"
RETRANSMIT_CHARACTERISTIC = "ff02"
OTA_VERSION = "1.0.1"
_NOTIFICATION_OPCODES = frozenset({0x17, 0x18, 0x25})
_STALE_PRN_OPCODES = frozenset({0x17})
_AUTHORIZATION_TOKEN = object()
_DEFAULT_CHUNK_PACING_SECONDS = 0.002
_COREBLUETOOTH_CHUNK_PACING_SECONDS = 0.010


class GattOtaError(RuntimeError):
    """A safe OTA transfer could not continue."""


class GattTimeoutError(GattOtaError):
    """A bounded GATT operation timed out."""


class GattDisconnectedError(GattOtaError):
    """The BLE link was lost during the transfer."""


class GattProtocolError(GattOtaError):
    """A device response did not match the confirmed protocol."""


class AuthorizationMode(Enum):
    FLASH = "flash"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedFirmware:
    data: bytes
    profile: firmware_image.FirmwareProfile
    mode: AuthorizationMode

    def __init__(
        self,
        data: bytes,
        profile: firmware_image.FirmwareProfile,
        mode: AuthorizationMode,
        *,
        _token: object,
    ) -> None:
        if _token is not _AUTHORIZATION_TOKEN:
            raise GattProtocolError(
                "firmware authorization must use authorize_firmware"
            )
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "mode", mode)


def authorize_firmware(
    data: bytes, vendor_model: str, *, recovery: bool
) -> AuthorizedFirmware:
    try:
        validated = firmware_image.validate_image(data)
    except firmware_image.ImageValidationError as exc:
        raise GattProtocolError("firmware image validation failed") from exc
    if not isinstance(vendor_model, str) or not vendor_model.startswith("B077T"):
        raise GattProtocolError("vendor model must have the B077T prefix")
    if recovery and validated.profile.kind is not firmware_image.ImageKind.GLOBAL:
        raise GattProtocolError("recovery authorization requires the GLOBAL image")
    mode = AuthorizationMode.RECOVERY if recovery else AuthorizationMode.FLASH
    return AuthorizedFirmware(
        data,
        validated.profile,
        mode,
        _token=_AUTHORIZATION_TOKEN,
    )


def authorize_configured_firmware(
    data: bytes,
    vendor_model: str,
    *,
    base_data: bytes,
    config: keymap_config.KeymapConfig,
    recovery: bool,
) -> AuthorizedFirmware:
    """Authorize only an exact in-memory regeneration from GLOBAL plus config."""

    if recovery:
        raise GattProtocolError("configured firmware cannot be used for recovery")
    try:
        validated = phase3_build_patch.validate_configured_target(
            base_data, data, config
        )
    except phase3_build_patch.FirmwarePatchError as exc:
        raise GattProtocolError("configured firmware validation failed") from exc
    if not isinstance(vendor_model, str) or not vendor_model.startswith("B077T"):
        raise GattProtocolError("vendor model must have the B077T prefix")
    return AuthorizedFirmware(
        data,
        validated.profile,
        AuthorizationMode.FLASH,
        _token=_AUTHORIZATION_TOKEN,
    )


def is_ota_notification(data: bytes) -> bool:
    frame = bytes(data)
    return bool(frame) and frame[0] in _NOTIFICATION_OPCODES


def corebluetooth_notification_options() -> dict[str, Any]:
    return {"cb": {"notification_discriminator": is_ota_notification}}


class GattOtaEngine:
    def __init__(
        self,
        client: Any,
        *,
        control_characteristic: Any = CONTROL_CHARACTERISTIC,
        retransmit_characteristic: Any = RETRANSMIT_CHARACTERISTIC,
        settle_seconds: float = 0.03,
        chunk_pacing_seconds: float = _DEFAULT_CHUNK_PACING_SECONDS,
        operation_timeout: float = 5.0,
        ack_timeout: float = 10.0,
        final_ack_timeout: float = 30.0,
    ) -> None:
        for name, value, allow_zero in (
            ("settle_seconds", settle_seconds, True),
            ("chunk_pacing_seconds", chunk_pacing_seconds, True),
            ("operation_timeout", operation_timeout, False),
            ("ack_timeout", ack_timeout, False),
            ("final_ack_timeout", final_ack_timeout, False),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a finite {qualifier} number")
        self._client = client
        self._control = control_characteristic
        self._retransmit = retransmit_characteristic
        self._settle_seconds = settle_seconds
        self._chunk_pacing_seconds = chunk_pacing_seconds
        self._operation_timeout = operation_timeout
        self._ack_timeout = ack_timeout
        self._final_ack_timeout = final_ack_timeout
        self._notifications: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue()
        self._payload_dispatch_counter = 0
        self._used = False
        self.last_state: pixart_ota.OtaState | None = None
        self.payload_mode = "write-without-response"
        self.host_wnr_limit: int | None = None
        self.physical_fragment_size: int | None = None
        self.wnr_ready_false_count = 0
        self.wnr_ready_wait_seconds = 0.0

    @staticmethod
    def _authorized_data(
        firmware: AuthorizedFirmware, expected_mode: AuthorizationMode
    ) -> bytes:
        if not isinstance(firmware, AuthorizedFirmware):
            raise GattProtocolError("firmware is not authorized")
        if firmware.mode is not expected_mode:
            raise GattProtocolError(
                f"firmware is not authorized for {expected_mode.value} mode"
            )
        return firmware.data

    def _notification_callback(self, _characteristic: Any, data: bytearray) -> None:
        self._notifications.put_nowait(
            (bytes(data), self._payload_dispatch_counter)
        )

    def _claim_once(self) -> None:
        if self._used:
            raise GattProtocolError("GATT OTA engine is single-use")
        self._used = True

    def _ensure_connected(self, stage: str) -> None:
        if not getattr(self._client, "is_connected", False):
            raise GattDisconnectedError(f"disconnected during {stage}")

    async def _bounded(self, awaitable: Any, stage: str, timeout: float | None = None):
        if not getattr(self._client, "is_connected", False):
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            raise GattDisconnectedError(f"disconnected during {stage}")
        try:
            result = await asyncio.wait_for(
                awaitable,
                timeout=self._operation_timeout if timeout is None else timeout,
            )
        except TimeoutError as exc:
            if not getattr(self._client, "is_connected", False):
                raise GattDisconnectedError(f"disconnected during {stage}") from exc
            raise GattTimeoutError(f"{stage} timed out") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not getattr(self._client, "is_connected", False):
                raise GattDisconnectedError(f"disconnected during {stage}") from exc
            raise GattOtaError(f"{stage} failed: {exc}") from exc
        self._ensure_connected(stage)
        return result

    async def _write(
        self, characteristic: Any, payload: bytes, *, response: bool
    ) -> None:
        if not response:
            await self._wait_for_wnr_ready(f"write 0x{payload[0]:02x}")
        await self._bounded(
            self._client.write_gatt_char(characteristic, payload, response=response),
            f"write 0x{payload[0]:02x}",
        )

    async def _write_payload(self, payload: bytes) -> None:
        if self.physical_fragment_size is None:
            raise GattProtocolError("physical fragment size is unavailable")
        for fragment_index, offset in enumerate(
            range(0, len(payload), self.physical_fragment_size)
        ):
            fragment = payload[offset : offset + self.physical_fragment_size]
            stage = f"write payload fragment {fragment_index} size {len(fragment)}"
            await self._wait_for_wnr_ready(stage)

            async def dispatch_payload(fragment: bytes = fragment) -> None:
                self._payload_dispatch_counter += 1
                await self._client.write_gatt_char(
                    self._control,
                    fragment,
                    response=False,
                )

            await self._bounded(dispatch_payload(), stage)
            await asyncio.sleep(self._payload_pacing_seconds())
            self._ensure_connected("payload pacing")

    def _payload_pacing_seconds(self) -> float:
        """Use conservative physical-fragment pacing on CoreBluetooth."""
        backend_id = getattr(self._client, "backend_id", None)
        if (
            getattr(backend_id, "value", backend_id) == "core_bluetooth"
            and self._chunk_pacing_seconds == _DEFAULT_CHUNK_PACING_SECONDS
        ):
            return _COREBLUETOOTH_CHUNK_PACING_SECONDS
        return self._chunk_pacing_seconds

    def _corebluetooth_wnr_ready(self):
        backend_id = getattr(self._client, "backend_id", None)
        if getattr(backend_id, "value", backend_id) != "core_bluetooth":
            return None
        missing = object()
        backend = getattr(self._client, "_backend", missing)
        if backend is missing:
            raise GattOtaError(
                "CoreBluetooth WNR readiness backend path is unavailable"
            )
        peripheral = getattr(backend, "_peripheral", missing)
        if peripheral is missing:
            raise GattOtaError(
                "CoreBluetooth WNR readiness peripheral path is unavailable"
            )
        ready = getattr(peripheral, "canSendWriteWithoutResponse", None)
        if not callable(ready):
            raise GattOtaError("CoreBluetooth WNR readiness API is unavailable")
        return ready

    async def _wait_for_wnr_ready(self, stage: str) -> None:
        ready = self._corebluetooth_wnr_ready()
        if ready is None:
            return
        wait_started: float | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._operation_timeout
        try:
            while True:
                if loop.time() >= deadline:
                    raise GattTimeoutError(f"{stage} WNR readiness timed out")
                self._ensure_connected(f"{stage} WNR readiness")
                try:
                    value = ready()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not getattr(self._client, "is_connected", False):
                        raise GattDisconnectedError(
                            f"disconnected during {stage} WNR readiness"
                        ) from exc
                    raise GattOtaError(
                        f"{stage} WNR readiness check failed: {exc}"
                    ) from exc
                self._ensure_connected(f"{stage} WNR readiness")
                if value is True:
                    return
                if value is not False:
                    raise GattOtaError(
                        f"{stage} WNR readiness returned a non-boolean value"
                    )
                self.wnr_ready_false_count += 1
                if wait_started is None:
                    wait_started = loop.time()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise GattTimeoutError(f"{stage} WNR readiness timed out")
                await asyncio.sleep(min(0.001, remaining))
        finally:
            if wait_started is not None:
                self.wnr_ready_wait_seconds += loop.time() - wait_started

    async def _write_reset(self, payload: bytes) -> None:
        await self._wait_for_wnr_ready("reset write")
        self._ensure_connected("reset write")
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(self._control, payload, response=False),
                timeout=self._operation_timeout,
            )
        except TimeoutError as exc:
            raise GattTimeoutError("reset write timed out") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise GattOtaError(f"reset write failed: {exc}") from exc

    async def _read_state(self, firmware_size: int) -> pixart_ota.OtaState:
        await self._write(
            self._control, pixart_ota.build_init_new(firmware_size), response=True
        )
        await self._bounded(asyncio.sleep(self._settle_seconds), "init settle")
        raw = bytes(
            await self._bounded(
                self._client.read_gatt_char(self._control), "init state read"
            )
        )
        try:
            return pixart_ota.parse_init_new_response(raw)
        except pixart_ota.ProtocolError as exc:
            raise GattProtocolError("invalid init-new response") from exc

    async def _wait_notification(
        self,
        expected_opcode: int,
        *,
        stage: str | None = None,
        minimum_payload_dispatch_counter: int | None = None,
        ignored_opcodes: frozenset[int] = frozenset(),
        timeout: float | None = None,
    ) -> bytes:
        wait_stage = stage or f"ACK 0x{expected_opcode:02x}"
        wait_timeout = self._ack_timeout if timeout is None else timeout
        deadline = asyncio.get_running_loop().time() + wait_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise GattTimeoutError(f"{wait_stage} timed out")
            frame, payload_dispatch_counter = await self._bounded(
                self._notifications.get(),
                wait_stage,
                remaining,
            )
            if (
                frame
                and frame[0] in ignored_opcodes
                and frame[0] == 0x17
                and len(frame) in (3, 4)
            ):
                continue
            if not frame or frame[0] != expected_opcode:
                raise GattProtocolError(
                    f"unexpected ACK during {wait_stage}; "
                    f"raw={frame.hex(' ') or '<empty>'}"
                )
            if (
                minimum_payload_dispatch_counter is not None
                and payload_dispatch_counter
                < minimum_payload_dispatch_counter
            ):
                continue
            return frame

    @staticmethod
    def _checksum_from_ack(frame: bytes, stage: str) -> int:
        if len(frame) == 3:
            return int.from_bytes(frame[1:3], "little")
        if len(frame) == 4:
            return int.from_bytes(frame[2:4], "little")
        raise GattProtocolError(f"malformed checksum ACK during {stage}")

    async def _wait_matching_checksum_notification(
        self,
        expected_checksum: int,
        *,
        stage: str,
        minimum_payload_dispatch_counter: int,
    ) -> bytes:
        deadline = asyncio.get_running_loop().time() + self._ack_timeout
        last_mismatch: tuple[int, bytes] | None = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                detail = ""
                if last_mismatch is not None:
                    received, frame = last_mismatch
                    detail = (
                        "; last checksum mismatch: "
                        f"expected=0x{expected_checksum:04X}, "
                        f"received=0x{received:04X}, raw={frame.hex(' ')}"
                    )
                raise GattTimeoutError(f"ACK 0x17 timed out{detail}")
            try:
                frame, payload_dispatch_counter = await self._bounded(
                    self._notifications.get(),
                    "ACK 0x17",
                    remaining,
                )
            except GattTimeoutError as exc:
                if last_mismatch is None:
                    raise
                received, frame = last_mismatch
                raise GattTimeoutError(
                    "ACK 0x17 timed out; last checksum mismatch: "
                    f"expected=0x{expected_checksum:04X}, "
                    f"received=0x{received:04X}, raw={frame.hex(' ')}"
                ) from exc
            if not frame or frame[0] != 0x17:
                raise GattProtocolError(
                    f"unexpected ACK during {stage}; "
                    f"raw={frame.hex(' ') or '<empty>'}"
                )
            checksum = self._checksum_from_ack(frame, stage)
            if checksum != expected_checksum:
                last_mismatch = (checksum, frame)
                continue
            if payload_dispatch_counter < minimum_payload_dispatch_counter:
                continue
            return frame

    def _drain_notifications(self) -> None:
        while True:
            try:
                self._notifications.get_nowait()
            except asyncio.QueueEmpty:
                return

    @staticmethod
    def _state_diagnostics(state: pixart_ota.OtaState) -> str:
        return (
            f"offset={state.offset}, checksum=0x{state.checksum:04X}, "
            f"max_object_size={state.max_object_size}, mtu_size={state.mtu_size}, "
            f"prn_threshold={state.prn_threshold}"
        )

    def _validate_host_wnr_limit(self, state: pixart_ota.OtaState) -> int:
        host_mtu = getattr(self._client, "mtu_size", None)
        if (
            not isinstance(host_mtu, int)
            or isinstance(host_mtu, bool)
            or host_mtu <= 3
        ):
            raise GattProtocolError(
                "host WNR limit is unavailable or invalid; "
                + self._state_diagnostics(state)
            )
        self.host_wnr_limit = host_mtu - 3
        transport_limit = min(state.mtu_size, self.host_wnr_limit)
        self.physical_fragment_size = transport_limit - (transport_limit % 4)
        if self.physical_fragment_size < 4:
            raise GattProtocolError(
                "aligned physical fragment size is below 4 bytes; "
                + self._state_diagnostics(state)
            )
        return self.physical_fragment_size

    def _validate_payload_transport(self, state: pixart_ota.OtaState) -> int:
        return self._validate_host_wnr_limit(state)

    def _chunk_diagnostics(
        self,
        state: pixart_ota.OtaState,
        object_index: int,
        chunk_index: int,
        chunk_length: int,
        stage: str,
    ) -> str:
        return (
            f"object {object_index} logical payload {chunk_index} length {chunk_length} "
            f"{stage}; {self._state_diagnostics(state)}; "
            f"payload mode={self.payload_mode}; "
            f"host WNR limit={self.host_wnr_limit}; "
            f"physical fragment size={self.physical_fragment_size}; "
            f"physical fragment pacing={self._payload_pacing_seconds():.3f}s; "
            f"WNR readiness false observations={self.wnr_ready_false_count}; "
            f"WNR readiness wait={self.wnr_ready_wait_seconds:.6f}s"
        )

    @staticmethod
    def _raise_with_context(error: GattOtaError, context: str) -> Never:
        raise type(error)(f"{context}: {error}") from error

    async def _run_transfer(
        self,
        firmware: bytes,
        state: pixart_ota.OtaState,
    ) -> None:
        prn_window_start = True
        object_index = state.offset - 1
        chunk_index = -1
        chunk_length = 0
        for operation in pixart_ota.iter_transfer_operations(
            firmware,
            state,
            OTA_VERSION,
        ):
            if operation.kind == "object-create":
                self._drain_notifications()
                prn_window_start = True
                object_index += 1
                chunk_index = -1
                await self._write(self._control, operation.payload, response=True)
            elif operation.kind == "wait-object":
                await self._wait_notification(
                    0x25, ignored_opcodes=_STALE_PRN_OPCODES
                )
            elif operation.kind == "payload":
                if prn_window_start:
                    self._drain_notifications()
                    prn_window_start = False
                chunk_index += 1
                chunk_length = len(operation.payload)
                try:
                    await self._write_payload(operation.payload)
                except GattOtaError as exc:
                    self._raise_with_context(
                        exc,
                        self._chunk_diagnostics(
                            state,
                            object_index,
                            chunk_index,
                            chunk_length,
                            "payload write",
                        ),
                    )
            elif operation.kind == "wait-prn":
                fragmented = self.physical_fragment_size < state.mtu_size
                if fragmented and not operation.object_end:
                    continue
                ack_stage = self._chunk_diagnostics(
                    state,
                    object_index,
                    chunk_index,
                    chunk_length,
                    "expected ACK 0x17",
                )
                try:
                    if fragmented:
                        frame = await self._wait_matching_checksum_notification(
                            operation.expected_checksum,
                            stage=ack_stage,
                            minimum_payload_dispatch_counter=(
                                self._payload_dispatch_counter
                            ),
                        )
                    else:
                        frame = await self._wait_notification(
                            0x17,
                            minimum_payload_dispatch_counter=(
                                self._payload_dispatch_counter
                            ),
                        )
                except GattOtaError as exc:
                    self._raise_with_context(exc, ack_stage)
                checksum = self._checksum_from_ack(frame, ack_stage)
                if checksum != operation.expected_checksum:
                    raise GattProtocolError(
                        "running checksum ACK does not match: "
                        f"expected=0x{operation.expected_checksum:04X}, "
                        f"received=0x{checksum:04X}, raw={frame.hex(' ')}; "
                        f"during {ack_stage}"
                    )
                prn_window_start = True
            elif operation.kind == "upgrade":
                self._drain_notifications()
                await self._write(self._control, operation.payload, response=True)
            elif operation.kind == "wait-upgrade":
                try:
                    frame = await self._wait_notification(
                        0x18,
                        ignored_opcodes=_STALE_PRN_OPCODES,
                        timeout=self._final_ack_timeout,
                    )
                except GattTimeoutError as exc:
                    raise GattTimeoutError(
                        "upgrade ACK 0x18 timed out; firmware payload transfer "
                        "completed; finalization outcome unknown; reset not sent; "
                        "do not resend; power-cycle then verify current OTA "
                        "version/checksum read-only"
                    ) from exc
                if len(frame) == 4 and frame[1] != 0:
                    rejection = (
                        "upgrade ACK reports failure: "
                        f"status=0x{frame[1]:02X}, raw={frame.hex(' ')}"
                    )
                    try:
                        rejection_state = await self._read_state(len(firmware))
                    except GattOtaError as exc:
                        raise GattProtocolError(
                            f"{rejection}; post-rejection state query failed: {exc}"
                        ) from exc
                    self.last_state = rejection_state
                    raise GattProtocolError(
                        f"{rejection}; post-rejection state: "
                        f"{self._state_diagnostics(rejection_state)}"
                    )
                if len(frame) not in (3, 4):
                    raise GattProtocolError("malformed upgrade ACK")
            elif operation.kind == "reset":
                await self._write_reset(operation.payload)
            else:
                raise GattProtocolError(
                    f"unsupported transfer operation: {operation.kind}"
                )

    async def _start_notifications(self) -> None:
        await self._bounded(
            self._client.start_notify(
                self._control,
                self._notification_callback,
                **corebluetooth_notification_options(),
            ),
            "start notify",
        )

    async def _stop_notifications(self) -> None:
        if not getattr(self._client, "is_connected", False):
            return
        await asyncio.wait_for(
            self._client.stop_notify(self._control),
            timeout=self._operation_timeout,
        )

    async def _cleanup_notifications(self, *, primary_failed: bool) -> None:
        try:
            await self._stop_notifications()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not getattr(self._client, "is_connected", False):
                return
            if not primary_failed:
                raise GattOtaError(f"stop notify failed: {exc}") from exc

    @staticmethod
    def resume_matches(firmware: bytes, state: pixart_ota.OtaState) -> bool:
        object_count = (
            len(firmware) + state.max_object_size - 1
        ) // state.max_object_size
        if state.offset > object_count:
            return False
        prefix_end = min(state.offset * state.max_object_size, len(firmware))
        return pixart_ota.sum16(firmware[:prefix_end]) == state.checksum

    async def inspect_state(self, firmware_size: int) -> pixart_ota.OtaState:
        """Read the resumable OTA state without notifications or state changes."""
        self._claim_once()
        state = await self._read_state(firmware_size)
        self.last_state = state
        return state

    async def flash(self, firmware: AuthorizedFirmware) -> pixart_ota.OtaState:
        data = self._authorized_data(firmware, AuthorizationMode.FLASH)
        self._claim_once()
        subscribed = False
        primary_failed = True
        try:
            await self._start_notifications()
            subscribed = True
            await self._write(
                self._retransmit, pixart_ota.build_retransmit(), response=True
            )
            state = await self._read_state(len(data))
            self.last_state = state
            self._validate_payload_transport(state)
            if state.offset != 0 or state.checksum != 0:
                raise GattProtocolError(
                    "retransmit did not clear resume state; "
                    + self._state_diagnostics(state)
                )
            await self._run_transfer(data, state)
            primary_failed = False
            return state
        finally:
            if subscribed:
                await self._cleanup_notifications(primary_failed=primary_failed)

    async def recover(self, firmware: AuthorizedFirmware) -> pixart_ota.OtaState:
        data = self._authorized_data(firmware, AuthorizationMode.RECOVERY)
        self._claim_once()
        subscribed = False
        primary_failed = True
        try:
            await self._start_notifications()
            subscribed = True
            await self._write(
                self._retransmit, pixart_ota.build_retransmit(), response=True
            )
            state = await self._read_state(len(data))
            self.last_state = state
            self._validate_payload_transport(state)
            if not self.resume_matches(data, state):
                raise GattProtocolError(
                    "retransmit checkpoint does not match firmware; "
                    + self._state_diagnostics(state)
                )
            await self._run_transfer(data, state)
            primary_failed = False
            return state
        finally:
            if subscribed:
                await self._cleanup_notifications(primary_failed=primary_failed)
