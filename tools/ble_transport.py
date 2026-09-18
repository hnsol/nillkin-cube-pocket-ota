"""Guarded BLE transport for the known read-only OTA commands."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

if __package__:
    from . import ota_protocol
else:
    import ota_protocol


class BleTransportError(RuntimeError):
    """A guarded BLE exchange failed."""


class UnsafeCommandError(BleTransportError):
    """A command is not part of the read-only allowlist."""


class TransportTimeoutError(BleTransportError):
    """A BLE operation exceeded its deadline."""


class DisconnectedError(BleTransportError):
    """The BLE link was lost during an exchange."""


class InvalidResponseError(BleTransportError):
    """A BLE response did not match the requested command."""


@dataclass(frozen=True)
class GattIdentity:
    advertised_name: str | None
    gatt_model: str | None
    gatt_revision: str | None
    service_uuids: tuple[str, ...]


@dataclass(frozen=True)
class ExchangeResult:
    command: ota_protocol.CommandSpec
    raw: bytes


def build_identity(
    *,
    advertised_name: str | None,
    gatt_model: str | None,
    gatt_revision: str | None,
    service_uuids: tuple[str, ...],
) -> GattIdentity:
    return GattIdentity(
        advertised_name=advertised_name,
        gatt_model=gatt_model,
        gatt_revision=gatt_revision,
        service_uuids=tuple(service_uuids),
    )


def is_expected_notification(data: bytes) -> bool:
    """Identify the vendor response envelope for Bleak's CoreBluetooth filter."""
    frame = bytes(data)
    return len(frame) >= 4 and frame[0] == 0x0E and frame[1] == len(frame) - 2


def corebluetooth_notification_options() -> dict[str, Any]:
    """Build future OTA ACK options for ``BleakClient.start_notify``.

    Phase 1 does not subscribe to notifications.  A later notify-based ACK path
    can pass this mapping as keyword arguments to ``start_notify``.
    """
    return {"cb": {"notification_discriminator": is_expected_notification}}


class BleTransport:
    def __init__(
        self,
        client: Any,
        characteristic: Any,
        *,
        settle_seconds: float = 0.2,
        operation_timeout: float = 5.0,
    ) -> None:
        self._client = client
        self._characteristic = characteristic
        self._settle_seconds = settle_seconds
        self._operation_timeout = operation_timeout

    async def _wait_for(self, awaitable: Any, operation: str) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout=self._operation_timeout)
        except TimeoutError as exc:
            raise TransportTimeoutError(
                f"{operation} timed out after {self._operation_timeout:g} seconds"
            ) from exc
        except Exception as exc:
            if isinstance(exc, ConnectionError) or not getattr(
                self._client, "is_connected", False
            ):
                raise DisconnectedError(f"disconnected during {operation}") from exc
            raise BleTransportError(f"{operation} failed: {exc}") from exc

    def _ensure_connected(self, stage: str) -> None:
        if not getattr(self._client, "is_connected", False):
            raise DisconnectedError(f"disconnected at {stage}")

    async def exchange(self, spec: ota_protocol.CommandSpec) -> ExchangeResult:
        if spec not in ota_protocol.READ_ONLY_COMMANDS.values():
            raise UnsafeCommandError(
                f"command 0x{spec.opcode:02x} is not in the read-only allowlist"
            )

        self._ensure_connected("exchange entry")
        response = spec.write_mode is ota_protocol.WriteMode.WITH_RESPONSE
        await self._wait_for(
            self._client.write_gatt_char(
                self._characteristic, spec.request, response=response
            ),
            f"write 0x{spec.opcode:02x}",
        )
        self._ensure_connected("after write")
        await self._wait_for(
            asyncio.sleep(self._settle_seconds),
            "settle",
        )
        self._ensure_connected("after settle")
        raw = bytes(
            await self._wait_for(
                self._client.read_gatt_char(self._characteristic),
                f"read 0x{spec.opcode:02x}",
            )
        )
        self._ensure_connected("after read")
        try:
            validated = ota_protocol.validate_response(spec, raw)
        except ota_protocol.ProtocolError as exc:
            raise InvalidResponseError(
                f"invalid response for command 0x{spec.opcode:02x}; "
                f"raw={raw.hex(' ') or '(empty)'}"
            ) from exc
        self._ensure_connected("before return")
        return ExchangeResult(command=spec, raw=validated)
