#!/usr/bin/env python3
"""Read Phase 1 BLE information from a Nillkin Cube Pocket keyboard.

This script has no firmware-transfer path.  The only GATT writes it permits are
the two observed information commands ``10 00`` and ``23 00``.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

if __package__:
    from . import ble_transport, ota_protocol
else:
    import ble_transport
    import ota_protocol


TARGET_NAMES = tuple(f"Cube Pocket Keyboard {unit}" for unit in (1, 2, 3))
TARGET_NAME = TARGET_NAMES[-1]
FF01_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
_BLUETOOTH_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"


class Phase1Error(RuntimeError):
    """Expected, safely reportable Phase 1 failure."""


class TargetNotFoundError(Phase1Error):
    """The named keyboard was not found during scanning."""


class GattValidationError(Phase1Error):
    """The connected device does not expose the expected GATT layout."""


@dataclass(frozen=True)
class CharacteristicInfo:
    uuid: str
    properties: tuple[str, ...]
    characteristic: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class Phase1Result:
    characteristics: dict[str, CharacteristicInfo]
    initial_read: bytes
    ota_init_response: bytes
    fw_info_response: bytes
    model_number: str | None = None
    firmware_revision: str | None = None
    model_identity: ota_protocol.ModelIdentity = (
        ota_protocol.ModelIdentity.UNAVAILABLE
    )


def normalize_uuid(value: str) -> str:
    """Normalize Bluetooth-base UUIDs to four hex digits for comparison."""
    normalized = str(value).strip().lower()
    if len(normalized) == 4 and all(c in "0123456789abcdef" for c in normalized):
        return normalized
    if (
        len(normalized) == 36
        and normalized.startswith("0000")
        and normalized[8:] == _BLUETOOTH_BASE_SUFFIX
    ):
        return normalized[4:8]
    return normalized


def inspect_gatt(services: Iterable[Any]) -> dict[str, CharacteristicInfo]:
    """Validate the expected ff00 service and return its required characteristics."""
    ff00_service = next(
        (service for service in services if normalize_uuid(service.uuid) == "ff00"),
        None,
    )
    if ff00_service is None:
        raise GattValidationError("必須Service ff00が見つかりません")

    found: dict[str, CharacteristicInfo] = {}
    for characteristic in ff00_service.characteristics:
        short_uuid = normalize_uuid(characteristic.uuid)
        if short_uuid in {"ff01", "ff02", "ff03"}:
            found[short_uuid] = CharacteristicInfo(
                uuid=short_uuid,
                properties=tuple(sorted(str(p).lower() for p in characteristic.properties)),
                characteristic=characteristic,
            )

    missing = sorted({"ff01", "ff02", "ff03"} - found.keys())
    if missing:
        raise GattValidationError(
            f"必須Characteristicが見つかりません: {', '.join(missing)}"
        )

    ff01_properties = set(found["ff01"].properties)
    if "read" not in ff01_properties or not {
        "write",
        "write-without-response",
    }.intersection(ff01_properties):
        raise GattValidationError(
            "ff01に必要なread/writeプロパティがありません: "
            + ", ".join(found["ff01"].properties)
        )
    return found


def _find_readable_characteristic(
    services: Iterable[Any], service_uuid: str, characteristic_uuid: str
) -> Any | None:
    service = next(
        (item for item in services if normalize_uuid(item.uuid) == service_uuid),
        None,
    )
    if service is None:
        return None
    return next(
        (
            characteristic
            for characteristic in service.characteristics
            if normalize_uuid(characteristic.uuid) == characteristic_uuid
            and "read" in {str(prop).lower() for prop in characteristic.properties}
        ),
        None,
    )


async def _read_optional_device_info(
    client: Any,
    services: Iterable[Any],
    characteristic_uuid: str,
    *,
    operation_timeout: float,
) -> str | None:
    characteristic = _find_readable_characteristic(
        services, "180a", characteristic_uuid
    )
    if characteristic is None:
        return None
    raw = await _with_timeout(
        client.read_gatt_char(characteristic),
        operation_timeout,
        f"Device Information {characteristic_uuid} read",
    )
    return bytes(raw).rstrip(b"\x00").decode("utf-8", errors="replace")


async def scan_target(scanner: Any, *, timeout: float) -> Any:
    def is_target(device: Any, advertisement_data: Any) -> bool:
        del advertisement_data
        return getattr(device, "name", None) in TARGET_NAMES

    try:
        device = await scanner.find_device_by_filter(is_target, timeout=timeout)
    except Exception as exc:
        raise Phase1Error(f"BLE scanに失敗しました: {exc}") from exc
    if device is None:
        raise TargetNotFoundError(
            f"{', '.join(TARGET_NAMES)} が{timeout:g}秒以内に見つかりませんでした"
        )
    return device


async def _with_timeout(awaitable: Any, timeout: float, operation: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise Phase1Error(f"{operation}が{timeout:g}秒でタイムアウトしました") from exc


async def collect_phase1_info(
    device: Any,
    *,
    client_factory: Callable[..., Any],
    settle_seconds: float = 0.2,
    operation_timeout: float = 5.0,
    connect_timeout: float = 10.0,
) -> Phase1Result:
    """Connect and execute the fixed, read-only Phase 1 command sequence."""
    try:
        async with client_factory(device, timeout=connect_timeout) as client:
            services = list(client.services)
            characteristics = inspect_gatt(services)
            model_number = await _read_optional_device_info(
                client,
                services,
                "2a24",
                operation_timeout=operation_timeout,
            )
            firmware_revision = await _read_optional_device_info(
                client,
                services,
                "2a26",
                operation_timeout=operation_timeout,
            )
            ff01 = characteristics["ff01"].characteristic
            initial = bytes(
                await _with_timeout(
                    client.read_gatt_char(ff01),
                    operation_timeout,
                    "ff01 初期read",
                )
            )
            transport = ble_transport.BleTransport(
                client,
                ff01,
                settle_seconds=settle_seconds,
                operation_timeout=operation_timeout,
            )
            ota_init = (
                await transport.exchange(ota_protocol.READ_ONLY_COMMANDS[0x10])
            ).raw
            fw_info = (
                await transport.exchange(ota_protocol.READ_ONLY_COMMANDS[0x23])
            ).raw
    except Phase1Error:
        raise
    except ble_transport.TransportTimeoutError as exc:
        raise Phase1Error(f"BLE操作がタイムアウトしました: {exc}") from exc
    except ble_transport.BleTransportError as exc:
        raise Phase1Error(f"安全なBLE交換に失敗しました: {exc}") from exc
    except Exception as exc:
        raise Phase1Error(f"BLE接続またはGATT操作に失敗しました: {exc}") from exc

    return Phase1Result(
        characteristics,
        initial,
        ota_init,
        fw_info,
        model_number,
        firmware_revision,
    )


def _ascii_candidates(data: bytes) -> list[str]:
    return [match.decode("ascii") for match in re.findall(rb"[ -~]{4,}", data)]


def parse_fw_info_response(data: bytes) -> tuple[str, int]:
    """Parse the fields used by OTAUtility.UpdateFwInfo."""
    try:
        info = ota_protocol.parse_firmware_info(data)
    except ota_protocol.ProtocolError as exc:
        raise Phase1Error("Get F/W Info応答の形式が一致しません") from exc
    return info.version, info.checksum


def print_result(result: Phase1Result) -> None:
    print("Service ff00: found")
    for uuid in ("ff01", "ff02", "ff03"):
        properties = ", ".join(result.characteristics[uuid].properties)
        print(f"Characteristic {uuid}: {properties}")
    print(f"GATT Model Number (2a24): {result.model_number}")
    print(f"GATT Firmware Revision (2a26): {result.firmware_revision}")
    print(f"Vendor OTA Model: {result.model_identity.value}")

    responses = (
        ("ff01 初期read", result.initial_read),
        ("10 00 応答", result.ota_init_response),
        ("23 00 Get F/W Info応答", result.fw_info_response),
    )
    for label, data in responses:
        print(f"{label}: {data.hex(' ') or '(empty)'}")
        candidates = _ascii_candidates(data)
        print("  ASCII候補: " + (" | ".join(candidates) if candidates else "(なし)"))

    try:
        ota_version, ota_checksum = parse_fw_info_response(result.fw_info_response)
    except Phase1Error:
        print("OTA Firmware Version: (応答形式不一致)")
        print("OTA checksum: (応答形式不一致)")
    else:
        print(f"OTA Firmware Version: {ota_version}")
        print(f"OTA checksum: 0x{ota_checksum:04X}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{TARGET_NAME}のPhase 1 BLE情報を安全に取得します"
    )
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--operation-timeout", type=float, default=5.0)
    parser.add_argument("--settle", type=float, default=0.2)
    return parser


def make_bleak_client_factory(
    client_class: Callable[..., Any], *, platform: str = sys.platform
) -> Callable[..., Any]:
    def factory(device: Any, **kwargs: Any) -> Any:
        if platform == "darwin":
            kwargs["cb"] = {
                "notification_discriminator": ble_transport.is_expected_notification
            }
        return client_class(device, **kwargs)

    return factory


async def _run(args: argparse.Namespace) -> Phase1Result:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise Phase1Error(
            "Bleakがありません。venvを有効化し pip install -r requirements.txt を実行してください"
        ) from exc

    print(f"scan: {', '.join(TARGET_NAMES)}（最大{args.scan_timeout:g}秒）")
    device = await scan_target(BleakScanner, timeout=args.scan_timeout)
    print(f"found: {getattr(device, 'name', TARGET_NAME)}")
    return await collect_phase1_info(
        device,
        client_factory=make_bleak_client_factory(BleakClient),
        settle_seconds=args.settle,
        operation_timeout=args.operation_timeout,
        connect_timeout=args.connect_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if min(
        args.scan_timeout,
        args.connect_timeout,
        args.operation_timeout,
        args.settle,
    ) < 0:
        print("error: timeout/settleには0以上を指定してください", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("中断しました。FWデータは送信していません。", file=sys.stderr)
        return 130
    except Phase1Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
