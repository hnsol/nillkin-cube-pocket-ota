"""Fail-closed PixArt OTA execution over the confirmed GATT endpoints."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

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
_AUTHORIZATION_TOKEN = object()


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
        operation_timeout: float = 5.0,
        ack_timeout: float = 10.0,
    ) -> None:
        for name, value, allow_zero in (
            ("settle_seconds", settle_seconds, True),
            ("operation_timeout", operation_timeout, False),
            ("ack_timeout", ack_timeout, False),
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
        self._operation_timeout = operation_timeout
        self._ack_timeout = ack_timeout
        self._notifications: asyncio.Queue[bytes] = asyncio.Queue()
        self._used = False

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
        self._notifications.put_nowait(bytes(data))

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
        await self._bounded(
            self._client.write_gatt_char(characteristic, payload, response=response),
            f"write 0x{payload[0]:02x}",
        )

    async def _write_reset(self, payload: bytes) -> None:
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

    async def _wait_notification(self, expected_opcode: int) -> bytes:
        frame = bytes(
            await self._bounded(
                self._notifications.get(),
                f"ACK 0x{expected_opcode:02x}",
                self._ack_timeout,
            )
        )
        if not frame or frame[0] != expected_opcode:
            raise GattProtocolError(
                f"unexpected ACK while waiting for 0x{expected_opcode:02x}"
            )
        return frame

    def _drain_notifications(self) -> None:
        while True:
            try:
                self._notifications.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _run_transfer(self, firmware: bytes, state: pixart_ota.OtaState) -> None:
        prn_window_start = True
        for operation in pixart_ota.iter_transfer_operations(
            firmware, state, OTA_VERSION
        ):
            if operation.kind == "object-create":
                self._drain_notifications()
                prn_window_start = True
                await self._write(self._control, operation.payload, response=True)
            elif operation.kind == "wait-object":
                await self._wait_notification(0x25)
            elif operation.kind == "payload":
                if prn_window_start:
                    self._drain_notifications()
                    prn_window_start = False
                await self._write(self._control, operation.payload, response=False)
            elif operation.kind == "wait-prn":
                frame = await self._wait_notification(0x17)
                if len(frame) == 3:
                    checksum = int.from_bytes(frame[1:3], "little")
                elif len(frame) == 4:
                    checksum = int.from_bytes(frame[2:4], "little")
                else:
                    raise GattProtocolError("malformed checksum ACK")
                if checksum != operation.expected_checksum:
                    raise GattProtocolError("running checksum ACK does not match")
                prn_window_start = True
            elif operation.kind == "upgrade":
                self._drain_notifications()
                await self._write(self._control, operation.payload, response=True)
            elif operation.kind == "wait-upgrade":
                frame = await self._wait_notification(0x18)
                if len(frame) == 4 and frame[1] != 0:
                    raise GattProtocolError("upgrade ACK reports failure")
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
            if not primary_failed:
                raise GattOtaError(f"stop notify failed: {exc}") from exc

    @staticmethod
    def _resume_matches(firmware: bytes, state: pixart_ota.OtaState) -> bool:
        object_count = (
            len(firmware) + state.max_object_size - 1
        ) // state.max_object_size
        if state.offset > object_count:
            return False
        prefix_end = min(state.offset * state.max_object_size, len(firmware))
        return pixart_ota.sum16(firmware[:prefix_end]) == state.checksum

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
            if state.offset != 0 or state.checksum != 0:
                raise GattProtocolError("retransmit did not clear resume state")
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
            state = await self._read_state(len(data))
            if not self._resume_matches(data, state):
                await self._write(
                    self._retransmit, pixart_ota.build_retransmit(), response=True
                )
                state = await self._read_state(len(data))
                if state.offset != 0 or state.checksum != 0:
                    raise GattProtocolError(
                        "retransmit did not establish zero recovery state"
                    )
            await self._run_transfer(data, state)
            primary_failed = False
            return state
        finally:
            if subscribed:
                await self._cleanup_notifications(primary_failed=primary_failed)
