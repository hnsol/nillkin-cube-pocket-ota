"""GLOBAL firmware recovery CLI for a still-advertising Cube Pocket keyboard.

This cannot recover a keyboard that no longer advertises over BLE.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from . import firmware_image, gatt_ota, macos_ota, phase1_ble_info, pixart_ota
else:
    import firmware_image
    import gatt_ota
    import macos_ota
    import phase1_ble_info
    import pixart_ota


class RecoveryPreflightError(RuntimeError):
    """GLOBAL recovery cannot safely begin."""


@dataclass(frozen=True)
class RecoveryStateReport:
    advertised_name: str
    gatt_model: str
    state: pixart_ota.OtaState
    prefix_matches: bool


async def inspect_state_on_client(
    client: Any,
    *,
    advertised_name: str,
    image: firmware_image.ValidatedImage,
    data: bytes,
    operation_timeout: float,
    settle_seconds: float = 0.03,
) -> RecoveryStateReport:
    """Read only the GLOBAL resume state using vendor command 0x27."""
    global_profile = firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.GLOBAL]
    if image.profile != global_profile:
        raise RecoveryPreflightError("診断には承認済みGLOBAL FWだけを指定できます")
    services = list(client.services)
    characteristics = phase1_ble_info.inspect_gatt(services)
    model = await phase1_ble_info._read_optional_device_info(
        client, services, "2a24", operation_timeout=operation_timeout
    )
    expected_model = global_profile.hardware_model.decode("ascii")
    if model != expected_model:
        raise RecoveryPreflightError(
            f"GATT modelが{expected_model}ではありません: {model or 'unavailable'}"
        )
    engine = gatt_ota.GattOtaEngine(
        client,
        control_characteristic=characteristics["ff01"].characteristic,
        settle_seconds=settle_seconds,
        operation_timeout=operation_timeout,
    )
    state = await engine.inspect_state(len(data))
    return RecoveryStateReport(
        advertised_name=advertised_name,
        gatt_model=model,
        state=state,
        prefix_matches=engine.resume_matches(data, state),
    )


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument(
        "--inspect-state",
        action="store_true",
        help="0x27状態照会だけを送りGLOBALの保持状態を表示する",
    )
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


async def _run_inspect(
    args: argparse.Namespace, image: firmware_image.ValidatedImage, data: bytes
) -> RecoveryStateReport:
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
        return await inspect_state_on_client(
            client,
            advertised_name=target.advertised_name,
            image=image,
            data=data,
            operation_timeout=args.operation_timeout,
        )


def print_state_report(report: RecoveryStateReport) -> None:
    state = report.state
    print(f"Advertised name: {report.advertised_name}")
    print(f"GATT model: {report.gatt_model}")
    print(f"Offset (objects): {state.offset}")
    print(f"Checksum: 0x{state.checksum:04X}")
    print(f"Max object size: {state.max_object_size}")
    print(f"MTU size: {state.mtu_size}")
    print(f"PRN threshold: {state.prn_threshold}")
    print("GLOBAL prefix match: " + ("yes" if report.prefix_matches else "no"))
    print("Result: 状態照会のみ完了。FW書込み・確定・再起動は未実施です。")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute and not args.inspect_state:
        print(
            "error: 復旧には--execute、状態照会には--inspect-stateが必要です",
            file=sys.stderr,
        )
        return 2
    if args.execute and not args.confirm_sha256:
        print("error: --executeには--confirm-sha256が必要です", file=sys.stderr)
        return 2
    if args.accept_factory_signature and not args.execute:
        print(
            "error: --accept-factory-signatureは--execute時だけ指定できます",
            file=sys.stderr,
        )
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
        if args.execute and args.confirm_sha256 != image.profile.sha256:
            print("error: --confirm-sha256が対象FWと完全一致しません", file=sys.stderr)
            return 2
        if args.inspect_state:
            state_report = asyncio.run(_run_inspect(args, image, data))
        else:
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
    if args.inspect_state:
        print_state_report(state_report)
        return 0
    macos_ota.print_report(report)
    print("Result: GLOBAL FWの復旧送信完了。機器の再起動・再接続を確認してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
