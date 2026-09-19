"""GLOBAL firmware recovery CLI for a still-advertising Cube Pocket keyboard.

This cannot recover a keyboard that no longer advertises over BLE.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path
from typing import Any

if __package__:
    from . import firmware_image, gatt_ota, macos_ota, phase1_ble_info
else:
    import firmware_image
    import gatt_ota
    import macos_ota
    import phase1_ble_info


class RecoveryPreflightError(RuntimeError):
    """GLOBAL recovery cannot safely begin."""


async def recover_on_client(
    client: Any,
    *,
    advertised_name: str,
    image: firmware_image.ValidatedImage,
    data: bytes,
    operation_timeout: float,
    accept_factory_signature: bool = False,
) -> macos_ota.PreflightReport:
    """Recover on a fresh, same-session read-only probe."""
    if image.profile.kind is not firmware_image.ImageKind.GLOBAL:
        raise RecoveryPreflightError("復旧には承認済みGLOBAL FWだけを指定できます")
    report = await macos_ota.collect_preflight_on_client(
        client,
        advertised_name=advertised_name,
        image=image,
        operation_timeout=operation_timeout,
        accept_factory_signature=accept_factory_signature,
    )
    if not report.ready_for_future_flash:
        raise RecoveryPreflightError(
            "recovery preflight gateを満たしません: " + "; ".join(report.blockers)
        )
    authorization_model = report.vendor_ota_model
    if authorization_model is None:
        global_profile = firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.GLOBAL]
        if not (
            accept_factory_signature
            and report.factory_signature_matched
            and image.profile == global_profile
        ):
            raise RecoveryPreflightError("Vendor OTA model B077Tを確認できません")
        authorization_model = global_profile.embedded_version.decode("ascii")
    authorized = gatt_ota.authorize_firmware(
        data, authorization_model, recovery=True
    )
    await gatt_ota.GattOtaEngine(client, operation_timeout=operation_timeout).recover(
        authorized
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BLE広告が残るCube Pocketを承認済みGLOBAL FWへ復旧する"
    )
    parser.add_argument("--firmware", required=True, metavar="PATH")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-sha256", metavar="SHA256")
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--operation-timeout", type=float, default=5.0)
    parser.add_argument(
        "--accept-factory-signature",
        action="store_true",
        help="確認済みの工場出荷signatureに限りGLOBAL復旧を許可する",
    )
    parser.add_argument(
        "--device-uuid",
        help="CoreBluetooth UUIDでscan対象を絞る（本人性の判定には使わない）",
    )
    return parser


async def _run(
    args: argparse.Namespace, image: firmware_image.ValidatedImage, data: bytes
) -> macos_ota.PreflightReport:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise phase1_ble_info.Phase1Error("Bleakがありません") from exc
    target = await macos_ota._scan_target(
        BleakScanner,
        timeout=args.scan_timeout,
        device_uuid=args.device_uuid,
    )
    factory = phase1_ble_info.make_bleak_client_factory(BleakClient)
    async with factory(target.device, timeout=args.connect_timeout) as client:
        return await recover_on_client(
            client,
            advertised_name=target.advertised_name,
            image=image,
            data=data,
            operation_timeout=args.operation_timeout,
            accept_factory_signature=args.accept_factory_signature,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print("error: 復旧を開始するには--executeが必要です", file=sys.stderr)
        return 2
    if not args.confirm_sha256:
        print("error: --executeには--confirm-sha256が必要です", file=sys.stderr)
        return 2
    timeouts = (args.scan_timeout, args.connect_timeout, args.operation_timeout)
    if not all(math.isfinite(value) and value > 0 for value in timeouts):
        print("error: timeoutには0より大きい値を指定してください", file=sys.stderr)
        return 2
    try:
        data = Path(args.firmware).read_bytes()
        image = firmware_image.validate_image(data)
        if image.profile.kind is not firmware_image.ImageKind.GLOBAL:
            raise RecoveryPreflightError("復旧には承認済みGLOBAL FWだけを指定できます")
        if args.confirm_sha256 != image.profile.sha256:
            print("error: --confirm-sha256が対象FWと完全一致しません", file=sys.stderr)
            return 2
        report = asyncio.run(_run(args, image, data))
    except (
        OSError,
        firmware_image.ImageValidationError,
        phase1_ble_info.Phase1Error,
        RecoveryPreflightError,
        gatt_ota.GattOtaError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("中断しました。復旧が未完了の可能性があります。", file=sys.stderr)
        return 130
    macos_ota.print_report(report)
    print("Result: GLOBAL FWの復旧送信完了。機器の再起動・再接続を確認してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
