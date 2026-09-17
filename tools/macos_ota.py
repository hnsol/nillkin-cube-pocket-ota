#!/usr/bin/env python3
"""Fail-closed, read-only macOS OTA preflight.

This module deliberately contains no firmware transfer or state-changing path.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

if __package__:
    from . import (
        ble_transport,
        firmware_image,
        ota_protocol,
        phase1_ble_info,
        pixart_ota,
    )
else:
    import ble_transport
    import firmware_image
    import ota_protocol
    import phase1_ble_info
    import pixart_ota


_REQUIRED_GATT = frozenset({"ff00", "ff01", "ff02", "ff03"})


@dataclass(frozen=True)
class PreflightReport:
    advertised_name: str | None
    gatt_model: str | None
    gatt_revision: str | None
    vendor_ota_model: str | None
    current_ota_version: str
    current_ota_checksum: int
    target_image_kind: str | None
    target_full_file_sum16: int | None
    checksums_comparable: bool
    ready_for_future_flash: bool
    blockers: tuple[str, ...]


def _is_approved_image(image: firmware_image.ValidatedImage | None) -> bool:
    if image is None:
        return False
    return any(
        image.profile == profile
        for profile in firmware_image.APPROVED_IMAGES.values()
    )


def _model_value(model: str | ota_protocol.ModelIdentity | None) -> str | None:
    return model if isinstance(model, str) else None


def evaluate_preflight(
    identity: ble_transport.GattIdentity,
    model: str | ota_protocol.ModelIdentity | None,
    current_fw: ota_protocol.OtaFirmwareInfo,
    image: firmware_image.ValidatedImage | None,
) -> PreflightReport:
    """Evaluate future write gates without performing any device I/O."""
    vendor_model = _model_value(model)
    services = {str(value).lower() for value in identity.service_uuids}
    blockers: list[str] = []

    if vendor_model is None or not vendor_model.startswith("B077T"):
        blockers.append("Vendor OTA model B077Tを確認できません")
    if identity.advertised_name not in phase1_ble_info.TARGET_NAMES:
        blockers.append("advertised nameがallowlistと一致しません")
    if identity.gatt_model != "PAR2801":
        blockers.append("GATT modelがPAR2801と一致しません")
    missing_gatt = sorted(_REQUIRED_GATT - services)
    if missing_gatt:
        blockers.append(
            "必須GATT構成が不足しています: " + ", ".join(missing_gatt)
        )
    if not _is_approved_image(image):
        blockers.append("firmware imageが承認済みではありません")

    return PreflightReport(
        advertised_name=identity.advertised_name,
        gatt_model=identity.gatt_model,
        gatt_revision=identity.gatt_revision,
        vendor_ota_model=vendor_model,
        current_ota_version=current_fw.version,
        current_ota_checksum=current_fw.checksum,
        target_image_kind=image.profile.kind.value if image is not None else None,
        target_full_file_sum16=(
            image.profile.full_file_sum16 if image is not None else None
        ),
        checksums_comparable=False,
        ready_for_future_flash=not blockers,
        blockers=tuple(blockers),
    )


async def collect_preflight(
    device: Any,
    *,
    advertised_name: str,
    client_factory: Callable[..., Any],
    image: firmware_image.ValidatedImage,
    settle_seconds: float = 0.2,
    operation_timeout: float = 5.0,
    connect_timeout: float = 10.0,
) -> PreflightReport:
    """Collect the fixed read-only Phase 1 sequence and evaluate its gates."""
    result = await phase1_ble_info.collect_phase1_info(
        device,
        client_factory=client_factory,
        settle_seconds=settle_seconds,
        operation_timeout=operation_timeout,
        connect_timeout=connect_timeout,
    )
    identity = ble_transport.build_identity(
        advertised_name=advertised_name,
        gatt_model=result.model_number,
        gatt_revision=result.firmware_revision,
        service_uuids=("ff00", *result.characteristics.keys()),
    )
    try:
        current_fw = ota_protocol.parse_firmware_info(result.fw_info_response)
    except ota_protocol.ProtocolError as exc:
        raise phase1_ble_info.Phase1Error(
            "Get F/W Info応答の形式が一致しません"
        ) from exc
    return evaluate_preflight(identity, result.model_identity, current_fw, image)


def print_report(report: PreflightReport) -> None:
    print(f"Advertised name: {report.advertised_name}")
    print(f"GATT model: {report.gatt_model}")
    print(f"GATT revision: {report.gatt_revision}")
    print(f"Vendor OTA model: {report.vendor_ota_model or 'unavailable'}")
    print(f"Current OTA version: {report.current_ota_version}")
    print(f"Current OTA checksum: 0x{report.current_ota_checksum:04X}")
    print(f"Target image: {report.target_image_kind}")
    if report.target_full_file_sum16 is not None:
        print(f"Target full-file sum16: 0x{report.target_full_file_sum16:04X}")
    print("Checksums comparable: no")
    if report.ready_for_future_flash:
        print("Result: 将来のwrite preflight gateを満たす")
    else:
        print("Result: 将来のwrite preflight gateを満たしません")
        for blocker in report.blockers:
            print(f"- {blocker}")


def _hex(data: bytes) -> str:
    return data.hex(" ")


def print_transfer_plan(
    image: firmware_image.ValidatedImage, data: bytes
) -> None:
    """Display only the statically known OTA wire plan; never performs I/O."""
    profile = image.profile
    init_new = pixart_ota.build_init_new(len(data))
    print(f"Target image: {profile.kind.value}")
    print(f"Target SHA-256: {profile.sha256}")
    print(f"Target size: {profile.size} bytes")
    print(f"Target full-file sum16: 0x{profile.full_file_sum16:04X}")
    print("Known OTA wire operations (not sent):")
    print(f"- 0x27 init-new send: {_hex(init_new)} (host→device; with response)")
    print("- 0x27 state response: ff01 read (device→host)")
    print(
        "- 0x25 object-create send: 実機0x27応答で決定 "
        "(host→device; with response)"
    )
    print("- 0x25 object ACK: notify待ち (device→host)")
    print(
        "- raw payload send: 実機0x27応答で決定 "
        "(host→device; without response)"
    )
    print("- 0x17 PRN ACK: notify待ち (device→host)")
    print(
        "- 0x18 upgrade send: version[10]が未確定 "
        "(host→device; with response)"
    )
    print("- 0x18 upgrade ACK: notify待ち (device→host)")
    print("- 0x22 reset send: 22 00 (host→device; without response)")
    print("Executable: no")
    print("- version[10]が未確定")
    print("- retransmit endpointが未確定")
    print("- MTU / PRN / resumeは実機0x27応答で決定")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Nillkin Cube Pocketのread-only OTA preflight"
    )
    parser.add_argument("--firmware", required=True, metavar="PATH")
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--operation-timeout", type=float, default=5.0)
    parser.add_argument(
        "--show-transfer-plan",
        action="store_true",
        help="承認済みFWの静的OTA転送計画だけを表示する（BLE未接続）",
    )
    return parser


async def _run(
    args: argparse.Namespace, image: firmware_image.ValidatedImage
) -> PreflightReport:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise phase1_ble_info.Phase1Error(
            "Bleakがありません。venvを有効化し "
            "pip install -r requirements.txt を実行してください"
        ) from exc

    target = await phase1_ble_info.scan_target(
        BleakScanner, timeout=args.scan_timeout
    )
    return await collect_preflight(
        target.device,
        advertised_name=target.advertised_name,
        client_factory=phase1_ble_info.make_bleak_client_factory(BleakClient),
        image=image,
        operation_timeout=args.operation_timeout,
        connect_timeout=args.connect_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    timeouts = (
        args.scan_timeout,
        args.connect_timeout,
        args.operation_timeout,
    )
    if not all(math.isfinite(value) and value > 0 for value in timeouts):
        print(
            "error: timeoutには0より大きい値を指定してください",
            file=sys.stderr,
        )
        return 2
    try:
        data = Path(args.firmware).read_bytes()
        image = firmware_image.validate_image(data)
        if args.show_transfer_plan:
            print_transfer_plan(image, data)
            return 0
        report = asyncio.run(_run(args, image))
    except (
        OSError,
        firmware_image.ImageValidationError,
        phase1_ble_info.Phase1Error,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "中断しました。FWデータは送信していません。",
            file=sys.stderr,
        )
        return 130
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
