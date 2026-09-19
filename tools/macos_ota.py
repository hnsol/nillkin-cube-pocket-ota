"""Fail-closed macOS OTA preflight and explicitly-confirmed updater."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from . import (
        ble_transport,
        firmware_image,
        gatt_ota,
        keymap_config,
        ota_protocol,
        phase1_ble_info,
        phase3_build_patch,
        pixart_ota,
    )
else:
    import ble_transport
    import firmware_image
    import gatt_ota
    import keymap_config
    import ota_protocol
    import phase1_ble_info
    import phase3_build_patch
    import pixart_ota


_REQUIRED_GATT = frozenset({"ff00", "ff01", "ff02", "ff03"})
_FACTORY_FW_INFO = bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61")
_FACTORY_NAME = "Cube Pocket Keyboard 3"
_INSTALLED_GLOBAL_FW_INFO = bytes.fromhex(
    "0e 09 23 00 31 2e 30 2e 31 27 ec"
)
_INSTALLED_REMAP_FW_INFO_HEADER = bytes.fromhex("0e 09 23 00") + b"1.0.1"


class ExecutePreflightError(RuntimeError):
    """A same-session write preflight gate did not pass."""


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
    factory_signature_matched: bool = False
    installed_global_signature_matched: bool = False
    installed_remap_signature_matched: bool = False


def _is_approved_image(image: firmware_image.ValidatedImage | None) -> bool:
    if image is None:
        return False
    if image.profile.kind is firmware_image.ImageKind.CONFIGURED:
        return True
    return any(
        image.profile == profile for profile in firmware_image.APPROVED_IMAGES.values()
    )


def _model_value(model: str | ota_protocol.ModelIdentity | None) -> str | None:
    return model if isinstance(model, str) else None


def _is_installed_signature_target(
    image: firmware_image.ValidatedImage | None,
) -> bool:
    if image is None:
        return False
    if image.profile.kind is firmware_image.ImageKind.CONFIGURED:
        return True
    return image.profile == firmware_image.APPROVED_IMAGES[
        firmware_image.ImageKind.JP_LANG
    ]


def _installed_remap_expected_sum16(
    config: keymap_config.KeymapConfig | None,
) -> int:
    """Compute the full-file sum16 the device would report for the target config."""
    if config is None:
        return firmware_image.APPROVED_IMAGES[
            firmware_image.ImageKind.JP_LANG
        ].full_file_sum16
    return phase3_build_patch.configured_spec(config).patched_sum16


def _installed_remap_fw_info(sum16: int) -> bytes:
    return _INSTALLED_REMAP_FW_INFO_HEADER + sum16.to_bytes(2, "little")


def resolve_installed_remap_fw_info(
    config: keymap_config.KeymapConfig | None,
) -> bytes:
    """Resolve the expected 0x23 raw response for the installed remapped image.

    Rejects an identity config (one whose sum16 equals GLOBAL's), since that
    signature must instead be recognized by --accept-installed-global-signature.
    """
    sum16 = _installed_remap_expected_sum16(config)
    global_sum16 = firmware_image.APPROVED_IMAGES[
        firmware_image.ImageKind.GLOBAL
    ].full_file_sum16
    if sum16 == global_sum16:
        raise ExecutePreflightError(
            "--installed-remap-configがGLOBALと同一の構成です。"
            "--accept-installed-global-signatureを使用してください"
        )
    return _installed_remap_fw_info(sum16)


def evaluate_preflight(
    identity: ble_transport.GattIdentity,
    model: str | ota_protocol.ModelIdentity | None,
    current_fw: ota_protocol.OtaFirmwareInfo,
    image: firmware_image.ValidatedImage | None,
    *,
    accept_factory_signature: bool = False,
    accept_installed_global_signature: bool = False,
    accept_installed_remap_signature: bool = False,
    installed_remap_fw_info: bytes | None = None,
) -> PreflightReport:
    """Evaluate future write gates without performing any device I/O."""
    vendor_model = _model_value(model)
    services = {str(value).lower() for value in identity.service_uuids}
    blockers: list[str] = []
    factory_signature_matched = bool(
        accept_factory_signature
        and vendor_model is None
        and identity.advertised_name == _FACTORY_NAME
        and identity.gatt_model == "PAR2801"
        and identity.gatt_revision == "1.0.0"
        and _REQUIRED_GATT.issubset(services)
        and current_fw.raw == _FACTORY_FW_INFO
        and image is not None
        and image.profile
        == firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.GLOBAL]
    )
    installed_global_signature_matched = bool(
        accept_installed_global_signature
        and vendor_model is None
        and identity.advertised_name in phase1_ble_info.TARGET_NAMES
        and identity.gatt_model == "PAR2801"
        and identity.gatt_revision == "1.0.0"
        and _REQUIRED_GATT.issubset(services)
        and current_fw.raw == _INSTALLED_GLOBAL_FW_INFO
        and _is_installed_signature_target(image)
    )
    installed_remap_signature_matched = bool(
        accept_installed_remap_signature
        and vendor_model is None
        and identity.advertised_name in phase1_ble_info.TARGET_NAMES
        and identity.gatt_model == "PAR2801"
        and identity.gatt_revision == "1.0.0"
        and _REQUIRED_GATT.issubset(services)
        and installed_remap_fw_info is not None
        and current_fw.raw == installed_remap_fw_info
        and _is_approved_image(image)
    )

    if (
        (vendor_model is None or not vendor_model.startswith("B077T"))
        and not factory_signature_matched
        and not installed_global_signature_matched
        and not installed_remap_signature_matched
    ):
        blockers.append("Vendor OTA model B077Tを確認できません")
    if identity.advertised_name not in phase1_ble_info.TARGET_NAMES:
        blockers.append("advertised nameがallowlistと一致しません")
    if identity.gatt_model != "PAR2801":
        blockers.append("GATT modelがPAR2801と一致しません")
    missing_gatt = sorted(_REQUIRED_GATT - services)
    if missing_gatt:
        blockers.append("必須GATT構成が不足しています: " + ", ".join(missing_gatt))
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
        factory_signature_matched=factory_signature_matched,
        installed_global_signature_matched=installed_global_signature_matched,
        installed_remap_signature_matched=installed_remap_signature_matched,
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
    probe_vendor_model: bool = False,
    accept_factory_signature: bool = False,
    accept_installed_global_signature: bool = False,
    accept_installed_remap_signature: bool = False,
    installed_remap_fw_info: bytes | None = None,
) -> PreflightReport:
    """Collect the fixed read-only Phase 1 sequence and evaluate its gates."""
    result = await phase1_ble_info.collect_phase1_info(
        device,
        client_factory=client_factory,
        settle_seconds=settle_seconds,
        operation_timeout=operation_timeout,
        connect_timeout=connect_timeout,
        probe_vendor_model=probe_vendor_model,
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
    return evaluate_preflight(
        identity,
        result.model_identity,
        current_fw,
        image,
        accept_factory_signature=accept_factory_signature,
        accept_installed_global_signature=accept_installed_global_signature,
        accept_installed_remap_signature=accept_installed_remap_signature,
        installed_remap_fw_info=installed_remap_fw_info,
    )


async def collect_preflight_on_client(
    client: Any,
    *,
    advertised_name: str,
    image: firmware_image.ValidatedImage,
    settle_seconds: float = 0.2,
    operation_timeout: float = 5.0,
    probe_vendor_model: bool = False,
    accept_factory_signature: bool = False,
    accept_installed_global_signature: bool = False,
    accept_installed_remap_signature: bool = False,
    installed_remap_fw_info: bytes | None = None,
) -> PreflightReport:
    """Run the confirmed read-only probe without reconnecting the client."""
    services = list(client.services)
    characteristics = phase1_ble_info.inspect_gatt(services)
    model_number = await phase1_ble_info._read_optional_device_info(
        client, services, "2a24", operation_timeout=operation_timeout
    )
    firmware_revision = await phase1_ble_info._read_optional_device_info(
        client, services, "2a26", operation_timeout=operation_timeout
    )
    ff01 = characteristics["ff01"].characteristic
    await phase1_ble_info._with_timeout(
        client.read_gatt_char(ff01), operation_timeout, "ff01 初期read"
    )
    transport = ble_transport.BleTransport(
        client,
        ff01,
        settle_seconds=settle_seconds,
        operation_timeout=operation_timeout,
    )
    try:
        await transport.exchange(ota_protocol.READ_ONLY_COMMANDS[0x10])
        fw_info = (
            await transport.exchange(ota_protocol.READ_ONLY_COMMANDS[0x23])
        ).raw
    except ble_transport.TransportTimeoutError as exc:
        raise phase1_ble_info.Phase1Error(
            f"BLE操作がタイムアウトしました: {exc}"
        ) from exc
    except ble_transport.BleTransportError as exc:
        raise phase1_ble_info.Phase1Error(
            f"安全なBLE交換に失敗しました: {exc}"
        ) from exc
    model: str | ota_protocol.ModelIdentity = ota_protocol.ModelIdentity.UNAVAILABLE
    if probe_vendor_model:
        try:
            model_count = (
                await transport.exchange(ota_protocol.READ_ONLY_COMMANDS[0x2A])
            ).raw
            ota_protocol.parse_model_count(model_count)
            model_info = (
                await transport.exchange(ota_protocol.READ_ONLY_COMMANDS[0x2B])
            ).raw
            model = ota_protocol.parse_model_info(model_info)
        except (ble_transport.BleTransportError, ota_protocol.ProtocolError) as exc:
            raise phase1_ble_info.Phase1Error(
                "Vendor OTA model probeに失敗しました"
            ) from exc
    try:
        current_fw = ota_protocol.parse_firmware_info(fw_info)
    except ota_protocol.ProtocolError as exc:
        raise phase1_ble_info.Phase1Error(
            "Get F/W Info応答の形式が一致しません"
        ) from exc
    identity = ble_transport.build_identity(
        advertised_name=advertised_name,
        gatt_model=model_number,
        gatt_revision=firmware_revision,
        service_uuids=("ff00", *characteristics.keys()),
    )
    return evaluate_preflight(
        identity,
        model,
        current_fw,
        image,
        accept_factory_signature=accept_factory_signature,
        accept_installed_global_signature=accept_installed_global_signature,
        accept_installed_remap_signature=accept_installed_remap_signature,
        installed_remap_fw_info=installed_remap_fw_info,
    )


async def execute_on_client(
    client: Any,
    *,
    advertised_name: str,
    image: firmware_image.ValidatedImage,
    data: bytes,
    base_data: bytes | None = None,
    config: keymap_config.KeymapConfig | None = None,
    operation_timeout: float,
    probe_vendor_model: bool = False,
    accept_factory_signature: bool = False,
    accept_installed_global_signature: bool = False,
    accept_installed_remap_signature: bool = False,
    installed_remap_fw_info: bytes | None = None,
) -> PreflightReport:
    """Authorize and flash only after a fresh probe on this exact connection."""
    if sum(
        (
            probe_vendor_model,
            accept_factory_signature,
            accept_installed_global_signature,
            accept_installed_remap_signature,
        )
    ) > 1:
        raise ExecutePreflightError(
            "model確認用のオプションは併用できません"
        )
    report = await collect_preflight_on_client(
        client,
        advertised_name=advertised_name,
        image=image,
        operation_timeout=operation_timeout,
        probe_vendor_model=probe_vendor_model,
        accept_factory_signature=accept_factory_signature,
        accept_installed_global_signature=accept_installed_global_signature,
        accept_installed_remap_signature=accept_installed_remap_signature,
        installed_remap_fw_info=installed_remap_fw_info,
    )
    if not report.ready_for_future_flash:
        raise ExecutePreflightError(
            "write preflight gateを満たしません: " + "; ".join(report.blockers)
        )
    if (base_data is None) != (config is None):
        raise ExecutePreflightError("configured firmware inputs must be paired")
    if (
        image.profile.kind is not firmware_image.ImageKind.GLOBAL
        and not probe_vendor_model
        and not accept_installed_global_signature
        and not accept_installed_remap_signature
    ):
        raise ExecutePreflightError(
            "JP_LANG/CONFIGUREDには--probe-vendor-model、"
            "--accept-installed-global-signatureまたは"
            "--accept-installed-remap-signatureが必要です"
        )
    authorization_model = report.vendor_ota_model
    if authorization_model is None:
        global_profile = firmware_image.APPROVED_IMAGES[firmware_image.ImageKind.GLOBAL]
        factory_fallback = (
            accept_factory_signature
            and report.factory_signature_matched
            and image.profile == global_profile
        )
        installed_global_fallback = (
            accept_installed_global_signature
            and report.installed_global_signature_matched
            and _is_installed_signature_target(image)
        )
        installed_remap_fallback = (
            accept_installed_remap_signature
            and report.installed_remap_signature_matched
            and _is_approved_image(image)
        )
        if not (
            factory_fallback
            or installed_global_fallback
            or installed_remap_fallback
        ):
            raise ExecutePreflightError("Vendor OTA model B077Tを確認できません")
        authorization_model = global_profile.embedded_version.decode("ascii")
    if config is None:
        authorized = gatt_ota.authorize_firmware(
            data, authorization_model, recovery=False
        )
    else:
        authorized = gatt_ota.authorize_configured_firmware(
            data,
            authorization_model,
            base_data=base_data,
            config=config,
            recovery=False,
        )
    await gatt_ota.GattOtaEngine(
        client, operation_timeout=operation_timeout
    ).flash(authorized)
    return report


def print_report(report: PreflightReport) -> None:
    print(f"Advertised name: {report.advertised_name}")
    print(f"GATT model: {report.gatt_model}")
    print(f"GATT revision: {report.gatt_revision}")
    print(f"Vendor OTA model: {report.vendor_ota_model or 'unavailable'}")
    print(
        "Factory signature: "
        + ("matched" if report.factory_signature_matched else "not used")
    )
    print(
        "Installed GLOBAL signature: "
        + (
            "matched"
            if report.installed_global_signature_matched
            else "not used"
        )
    )
    print(
        "Installed remap signature: "
        + (
            "matched"
            if report.installed_remap_signature_matched
            else "not used"
        )
    )
    print(f"Current OTA version: {report.current_ota_version}")
    print(f"Current OTA checksum: 0x{report.current_ota_checksum:04X}")
    print(f"Target image: {report.target_image_kind}")
    if report.target_full_file_sum16 is not None:
        print(f"Target full-file sum16: 0x{report.target_full_file_sum16:04X}")
    print("Checksums comparable: no")
    if report.ready_for_future_flash:
        print("Result: 将来のwrite preflight gateを満たす")
    elif report.blockers == ("Vendor OTA model B077Tを確認できません",):
        global_sum = firmware_image.APPROVED_IMAGES[
            firmware_image.ImageKind.GLOBAL
        ].full_file_sum16
        jp_sum = firmware_image.APPROVED_IMAGES[
            firmware_image.ImageKind.JP_LANG
        ].full_file_sum16
        if report.current_ota_version == "1.0.1" and (
            report.current_ota_checksum == global_sum
        ):
            print("Result: read-only確認完了。FW書込みは未実施です。")
            print(
                "既知のGLOBALを検出しました。JP_LANG/設定生成FWへ進むには "
                "--accept-installed-global-signature を明示してください。"
            )
        elif report.current_ota_version == "1.0.1" and (
            report.current_ota_checksum == jp_sum
        ):
            print("Result: read-only確認完了。FW書込みは未実施です。")
            print(
                "既知のremap checksumを検出しました。再書込みには "
                "--accept-installed-remap-signature を明示してください。"
            )
        else:
            print("Result: 将来のwrite preflight gateを満たしません")
            for blocker in report.blockers:
                print(f"- {blocker}")
    else:
        print("Result: 将来のwrite preflight gateを満たしません")
        for blocker in report.blockers:
            print(f"- {blocker}")


def _hex(data: bytes) -> str:
    return data.hex(" ")


def print_transfer_plan(image: firmware_image.ValidatedImage, data: bytes) -> None:
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
    print("- 0x25 object-create send: 実機0x27応答で決定 (host→device; with response)")
    print("- 0x25 object ACK: notify待ち (device→host)")
    print("- raw payload send: 実機0x27応答で決定 (host→device; without response)")
    print("- 0x17 PRN ACK: notify待ち (device→host)")
    print("- 0x18 upgrade send: version[5]=1.0.1 (host→device; with response)")
    print("- 0x18 upgrade ACK: notify待ち (device→host)")
    print("- 0x22 reset send: 22 00 (host→device; without response)")
    print("Executable: --executeとSHA-256確認時のみ")
    print("- retransmit: ff02へ0x28")
    print("- MTU / PRN / resumeは実機0x27応答で決定")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Nillkin Cube Pocketのread-only OTA preflight"
    )
    parser.add_argument("--firmware", required=True, metavar="PATH")
    parser.add_argument(
        "--base-firmware",
        metavar="PATH",
        help="configured targetを再生成・照合する公式GLOBAL原本",
    )
    parser.add_argument(
        "--remap-config",
        metavar="PATH",
        help="configured targetを再生成・照合するTOML設定",
    )
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--operation-timeout", type=float, default=5.0)
    parser.add_argument(
        "--device-uuid",
        help="CoreBluetooth UUIDでscan対象を絞る（本人性の判定には使わない）",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="同一接続で再取得したpreflight通過後にだけFWを書き込む",
    )
    parser.add_argument(
        "--accept-factory-signature",
        action="store_true",
        help="既知factory fingerprintから固定GLOBALへの初回書込みだけを許可する",
    )
    parser.add_argument(
        "--accept-installed-global-signature",
        action="store_true",
        help="導入済みGLOBAL fingerprintからJP_LANG/設定生成FWへの書込みを許可する",
    )
    parser.add_argument(
        "--accept-installed-remap-signature",
        action="store_true",
        help=(
            "導入済みJP_LANG/設定生成FW fingerprintからGLOBAL/JP_LANG/"
            "設定生成FWへの書込みを許可する"
        ),
    )
    parser.add_argument(
        "--installed-remap-config",
        metavar="PATH",
        help=(
            "--accept-installed-remap-signatureで導入済みFWのchecksumを"
            "算出するTOML設定（省略時は固定JP_LANGを仮定する）"
        ),
    )
    parser.add_argument(
        "--probe-vendor-model",
        action="store_true",
        help="0x10→0x23後に同一接続で0x2A→0x2Bを明示的に試す",
    )
    parser.add_argument(
        "--confirm-sha256",
        metavar="SHA256",
        help="--execute時に対象FWのSHA-256を完全一致で指定する",
    )
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

    target = await _scan_target(
        BleakScanner,
        timeout=args.scan_timeout,
        device_uuid=getattr(args, "device_uuid", None),
    )
    return await collect_preflight(
        target.device,
        advertised_name=target.advertised_name,
        client_factory=phase1_ble_info.make_bleak_client_factory(BleakClient),
        image=image,
        operation_timeout=args.operation_timeout,
        connect_timeout=args.connect_timeout,
        probe_vendor_model=getattr(args, "probe_vendor_model", False),
    )


async def _scan_target(
    scanner: Any, *, timeout: float, device_uuid: str | None
) -> phase1_ble_info.DiscoveredTarget:
    if device_uuid is None:
        return await phase1_ble_info.scan_target(scanner, timeout=timeout)
    wanted = device_uuid.lower()
    matched_name: str | None = None

    def matches(device: Any, advertisement_data: Any) -> bool:
        nonlocal matched_name
        if str(getattr(device, "address", "")).lower() != wanted:
            return False
        local_name = getattr(advertisement_data, "local_name", None)
        if local_name not in phase1_ble_info.TARGET_NAMES:
            return False
        matched_name = local_name
        return True

    device = await scanner.find_device_by_filter(matches, timeout=timeout)
    if device is None or matched_name is None:
        raise phase1_ble_info.TargetNotFoundError(
            "指定UUIDかつCube Pocket Keyboardの広告が見つかりません"
        )
    return phase1_ble_info.DiscoveredTarget(device, matched_name)


async def _run_execute(
    args: argparse.Namespace,
    image: firmware_image.ValidatedImage,
    data: bytes,
    *,
    base_data: bytes | None = None,
    config: keymap_config.KeymapConfig | None = None,
    installed_remap_fw_info: bytes | None = None,
) -> PreflightReport:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise phase1_ble_info.Phase1Error("Bleakがありません") from exc
    target = await _scan_target(
        BleakScanner,
        timeout=args.scan_timeout,
        device_uuid=getattr(args, "device_uuid", None),
    )
    factory = phase1_ble_info.make_bleak_client_factory(BleakClient)
    async with factory(target.device, timeout=args.connect_timeout) as client:
        return await execute_on_client(
            client,
            advertised_name=target.advertised_name,
            image=image,
            data=data,
            base_data=base_data,
            config=config,
            operation_timeout=args.operation_timeout,
            probe_vendor_model=getattr(args, "probe_vendor_model", False),
            accept_factory_signature=getattr(
                args, "accept_factory_signature", False
            ),
            accept_installed_global_signature=getattr(
                args, "accept_installed_global_signature", False
            ),
            accept_installed_remap_signature=getattr(
                args, "accept_installed_remap_signature", False
            ),
            installed_remap_fw_info=installed_remap_fw_info,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.accept_factory_signature and not args.execute:
        print(
            "error: --accept-factory-signatureは--execute時だけ指定できます",
            file=sys.stderr,
        )
        return 2
    if args.accept_installed_global_signature and not args.execute:
        print(
            "error: --accept-installed-global-signatureは--execute時だけ指定できます",
            file=sys.stderr,
        )
        return 2
    if args.accept_installed_remap_signature and not args.execute:
        print(
            "error: --accept-installed-remap-signatureは--execute時だけ指定できます",
            file=sys.stderr,
        )
        return 2
    if args.installed_remap_config and not args.accept_installed_remap_signature:
        print(
            "error: --installed-remap-configは"
            "--accept-installed-remap-signatureと組で指定してください",
            file=sys.stderr,
        )
        return 2
    if sum(
        (
            args.accept_factory_signature,
            args.accept_installed_global_signature,
            args.accept_installed_remap_signature,
            args.probe_vendor_model,
        )
    ) > 1:
        print(
            "error: model確認用のオプションは併用できません",
            file=sys.stderr,
        )
        return 2
    if bool(args.base_firmware) != bool(args.remap_config):
        print(
            "error: --base-firmwareと--remap-configは常に組で指定してください",
            file=sys.stderr,
        )
        return 2
    if args.execute and not args.confirm_sha256:
        print("error: --executeには--confirm-sha256が必要です", file=sys.stderr)
        return 2
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
        base_data: bytes | None = None
        config: keymap_config.KeymapConfig | None = None
        if args.base_firmware:
            base_data = Path(args.base_firmware).read_bytes()
            config = keymap_config.load_config(args.remap_config)
            image = phase3_build_patch.validate_configured_target(
                base_data, data, config
            )
        else:
            image = firmware_image.validate_image(data)
        if args.execute and args.confirm_sha256 != image.profile.sha256:
            print("error: --confirm-sha256が対象FWと完全一致しません", file=sys.stderr)
            return 2
        if args.show_transfer_plan:
            if args.execute:
                print(
                    "error: --show-transfer-planと--executeは併用できません",
                    file=sys.stderr,
                )
                return 2
            print_transfer_plan(image, data)
            return 0
        installed_remap_fw_info: bytes | None = None
        if args.accept_installed_remap_signature:
            installed_remap_config = (
                keymap_config.load_config(args.installed_remap_config)
                if args.installed_remap_config
                else None
            )
            try:
                installed_remap_fw_info = resolve_installed_remap_fw_info(
                    installed_remap_config
                )
            except ExecutePreflightError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        report = asyncio.run(
            _run_execute(
                args,
                image,
                data,
                base_data=base_data,
                config=config,
                installed_remap_fw_info=installed_remap_fw_info,
            )
            if args.execute
            else _run(args, image)
        )
    except (
        OSError,
        firmware_image.ImageValidationError,
        keymap_config.KeymapConfigError,
        phase3_build_patch.FirmwarePatchError,
        phase1_ble_info.Phase1Error,
        ExecutePreflightError,
        gatt_ota.GattOtaError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        message = (
            "中断しました。OTAが未完了の可能性があります。"
            if args.execute
            else "中断しました。FWデータは送信していません。"
        )
        print(message, file=sys.stderr)
        return 130
    print_report(report)
    if args.execute:
        print("Result: OTA送信完了。機器の再起動・再接続を確認してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
