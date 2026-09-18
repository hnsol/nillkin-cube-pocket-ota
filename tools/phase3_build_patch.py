"""Build a strictly validated Nillkin keymap firmware patch.

This tool only reads and writes firmware files. It contains no BLE/OTA code.
"""

import argparse
import hashlib
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from . import firmware_image as images
    from . import keymap_config
else:
    import firmware_image as images
    import keymap_config


class FirmwarePatchError(RuntimeError):
    """Firmware validation or safe output creation failed."""


@dataclass(frozen=True)
class BytePatch:
    offset: int
    old: int
    new: int
    label: str


@dataclass(frozen=True)
class FirmwareSpec:
    size: int
    sha256: str
    version: bytes
    patches: tuple[BytePatch, ...]
    patched_sum16: int
    approved_kind: images.ImageKind | None = None
    require_keymap_high_zero: bool = False


@dataclass(frozen=True)
class BuildResult:
    size: int
    original_sha256: str
    original_sum16: int
    patched_sha256: str
    patched_sum16: int
    original_path: Path
    patched_path: Path
    differences: tuple[tuple[int, int, int], ...]


_GLOBAL_PROFILE = images.APPROVED_IMAGES[images.ImageKind.GLOBAL]
_JP_LANG_PROFILE = images.APPROVED_IMAGES[images.ImageKind.JP_LANG]

GLOBAL_SPEC = FirmwareSpec(
    size=_GLOBAL_PROFILE.size,
    sha256=_GLOBAL_PROFILE.sha256,
    version=_GLOBAL_PROFILE.embedded_version,
    patches=(
        BytePatch(0x1DABE, 0x39, 0xE0, "Caps"),
        BytePatch(0x1DB4A, 0xE0, 0xE2, "Left Ctrl"),
        BytePatch(0x1DB3E, 0xE2, 0xE3, "Left Alt"),
        BytePatch(0x1DB56, 0xE3, 0x91, "Left Cmd"),
        BytePatch(0x1DB58, 0xE7, 0x90, "Right Cmd"),
        BytePatch(0x1DB4C, 0xE6, 0xE7, "Right Alt"),
    ),
    patched_sum16=_JP_LANG_PROFILE.full_file_sum16,
    approved_kind=images.ImageKind.GLOBAL,
    require_keymap_high_zero=True,
)


_PHYSICAL_KEY_PATCHES = {
    "caps_lock": BytePatch(0x1DABE, 0x39, 0xE0, "Caps"),
    "left_control": BytePatch(0x1DB4A, 0xE0, 0xE2, "Left Ctrl"),
    "left_alt": BytePatch(0x1DB3E, 0xE2, 0xE3, "Left Alt"),
    "left_gui": BytePatch(0x1DB56, 0xE3, 0x91, "Left Cmd"),
    "right_gui": BytePatch(0x1DB58, 0xE7, 0x90, "Right Cmd"),
    "right_alt": BytePatch(0x1DB4C, 0xE6, 0xE7, "Right Alt"),
}


def configured_spec(config: keymap_config.KeymapConfig) -> FirmwareSpec:
    """Build an exact GLOBAL-derived patch specification from a checked TOML config."""

    patches = tuple(
        BytePatch(
            offset=source.offset,
            old=source.old,
            new=usage,
            label=source.label,
        )
        for name, usage in config.remap.items()
        if usage != (source := _PHYSICAL_KEY_PATCHES[name]).old
    )
    patched_sum16 = (
        _GLOBAL_PROFILE.full_file_sum16
        + sum(patch.new - patch.old for patch in patches)
    ) & 0xFFFF
    return FirmwareSpec(
        size=_GLOBAL_PROFILE.size,
        sha256=_GLOBAL_PROFILE.sha256,
        version=_GLOBAL_PROFILE.embedded_version,
        patches=patches,
        patched_sum16=patched_sum16,
        approved_kind=images.ImageKind.GLOBAL,
        require_keymap_high_zero=True,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_original(data: bytes, spec: FirmwareSpec = GLOBAL_SPEC) -> None:
    if spec.approved_kind is not None:
        try:
            validated = images.validate_image(data)
        except images.ImageValidationError as error:
            raise FirmwarePatchError(str(error)) from error
        if validated.profile.kind is not spec.approved_kind:
            raise FirmwarePatchError(
                f"firmware kind mismatch: expected {spec.approved_kind.value}, "
                f"got {validated.profile.kind.value}"
            )
    if len(data) != spec.size:
        raise FirmwarePatchError(
            f"original size mismatch: expected {spec.size}, got {len(data)}"
        )
    digest = _sha256(data)
    if digest != spec.sha256:
        raise FirmwarePatchError(
            f"original SHA-256 mismatch: expected {spec.sha256}, got {digest}"
        )
    occurrences = data.count(spec.version)
    if occurrences != 1:
        raise FirmwarePatchError(
            f"version {spec.version.decode('ascii')} must occur exactly once; "
            f"found {occurrences}"
        )
    for change in spec.patches:
        actual = data[change.offset]
        if actual != change.old:
            raise FirmwarePatchError(
                f"offset 0x{change.offset:x}: expected 0x{change.old:02x}, "
                f"got 0x{actual:02x}"
            )
        if spec.require_keymap_high_zero:
            high_byte = data[change.offset + 1]
            if high_byte != 0:
                raise FirmwarePatchError(
                    f"offset 0x{change.offset + 1:x}: expected keymap high byte "
                    f"0x00, got 0x{high_byte:02x}"
                )


def _differences(before: bytes, after: bytes) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (offset, old, new)
        for offset, (old, new) in enumerate(zip(before, after, strict=True))
        if old != new
    )


def verify_patched(
    original: bytes, patched: bytes, spec: FirmwareSpec = GLOBAL_SPEC
) -> tuple[tuple[int, int, int], ...]:
    if len(patched) != len(original) or len(patched) != spec.size:
        raise FirmwarePatchError("patched size differs from the validated original")
    actual = _differences(original, patched)
    expected = tuple(sorted((item.offset, item.old, item.new) for item in spec.patches))
    if actual != expected:
        raise FirmwarePatchError(
            f"patched image has unexpected differences: expected {expected!r}, "
            f"got {actual!r}"
        )
    checksum = sum(patched) & 0xFFFF
    if checksum != spec.patched_sum16:
        raise FirmwarePatchError(
            f"patched sum16 mismatch: expected 0x{spec.patched_sum16:04x}, "
            f"got 0x{checksum:04x}"
        )
    return actual


def patch_firmware(data: bytes, spec: FirmwareSpec = GLOBAL_SPEC) -> bytes:
    validate_original(data, spec)
    patched = bytearray(data)
    for change in spec.patches:
        patched[change.offset] = change.new
    result = bytes(patched)
    verify_patched(data, result, spec)
    return result


def patch_configured_firmware(
    data: bytes, config: keymap_config.KeymapConfig
) -> tuple[bytes, FirmwareSpec]:
    """Patch verified GLOBAL bytes using an already parsed configuration."""

    spec = configured_spec(config)
    return patch_firmware(data, spec), spec


def validate_configured_target(
    base_data: bytes, target_data: bytes, config: keymap_config.KeymapConfig
) -> images.ValidatedImage:
    """Accept only a target exactly regenerated from GLOBAL and this config."""

    expected, _ = patch_configured_firmware(base_data, config)
    if target_data != expected:
        raise FirmwarePatchError(
            "configured target does not match GLOBAL regeneration from this config"
        )
    try:
        return images.validate_configured_image(target_data)
    except images.ImageValidationError as error:
        raise FirmwarePatchError(str(error)) from error


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(
        strict=False
    )


def validate_patched_name(name: str) -> str:
    """Accept a filename only, so config/CLI input cannot choose another directory."""

    path = Path(name)
    if path.name != name or path.suffix.lower() != ".bin" or name == ".bin":
        raise FirmwarePatchError("patched output must be a .bin filename")
    return name


def default_patched_name(config_path: Path) -> str:
    """Derive a stable, visible output name from a remap config filename."""

    stem = config_path.stem.upper().replace("-", "_")
    return validate_patched_name(f"B077T_US_13_{stem}.bin")


def _stage_file(destination: Path, data: bytes) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_without_overwrite(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination)
    except FileExistsError as error:
        raise FirmwarePatchError(f"output already exists: {destination}") from error
    finally:
        temporary.unlink(missing_ok=True)


def build_files(
    source: str | Path,
    original_output: str | Path,
    patched_output: str | Path,
    *,
    spec: FirmwareSpec = GLOBAL_SPEC,
) -> BuildResult:
    source = Path(source)
    original_output = Path(original_output)
    patched_output = Path(patched_output)
    if _same_path(source, original_output) or _same_path(source, patched_output):
        raise FirmwarePatchError("input and output must not be the same path")
    if _same_path(original_output, patched_output):
        raise FirmwarePatchError(
            "original and patched outputs must not be the same path"
        )
    for destination in (original_output, patched_output):
        if os.path.lexists(destination):
            raise FirmwarePatchError(f"output already exists: {destination}")

    original = source.read_bytes()
    patched = patch_firmware(original, spec)
    differences = verify_patched(original, patched, spec)

    original_temp = _stage_file(original_output, original)
    patched_temp = _stage_file(patched_output, patched)
    original_published = False
    try:
        _publish_without_overwrite(original_temp, original_output)
        original_published = True
        _publish_without_overwrite(patched_temp, patched_output)
    except BaseException:
        original_temp.unlink(missing_ok=True)
        patched_temp.unlink(missing_ok=True)
        if original_published:
            original_output.unlink(missing_ok=True)
        raise

    copied = original_output.read_bytes()
    if copied != original:
        raise FirmwarePatchError("saved original copy does not match input")
    return BuildResult(
        size=len(original),
        original_sha256=_sha256(original),
        original_sum16=sum(original) & 0xFFFF,
        patched_sha256=_sha256(patched),
        patched_sum16=sum(patched) & 0xFFFF,
        original_path=original_output,
        patched_path=patched_output,
        differences=differences,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate GLOBAL firmware and build a checked keymap patch"
    )
    parser.add_argument("firmware", type=Path, help="downloaded GLOBAL firmware")
    parser.add_argument("--output-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config", type=Path, help="TOML remap configuration (format_version = 1)"
    )
    parser.add_argument(
        "--patched-name",
        help="patched filename only; default derives from --config or uses JP_LANG",
    )
    args = parser.parse_args(argv)
    if args.patched_name is not None:
        patched_name = validate_patched_name(args.patched_name)
    elif args.config is not None:
        patched_name = default_patched_name(args.config)
    else:
        patched_name = "B077T_US_13_JP_LANG.bin"
    spec = (
        configured_spec(keymap_config.load_config(args.config))
        if args.config is not None
        else GLOBAL_SPEC
    )
    original_path = args.output_root / "firmware" / "original" / "B077T_US_13.bin"
    patched_path = args.output_root / "firmware" / "patched" / patched_name
    result = build_files(args.firmware, original_path, patched_path, spec=spec)

    print(f"size: {result.size} bytes")
    print(f"original: {result.original_path}")
    print(f"original SHA-256: {result.original_sha256}")
    print(f"original sum16: 0x{result.original_sum16:04x}")
    print(f"patched: {result.patched_path}")
    print(f"patched SHA-256: {result.patched_sha256}")
    print(f"patched sum16: 0x{result.patched_sum16:04x}")
    print("differences:")
    labels = {change.offset: change.label for change in spec.patches}
    for offset, old, new in result.differences:
        print(f"  0x{offset:x}: 0x{old:02x} -> 0x{new:02x} ({labels[offset]})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FirmwarePatchError, keymap_config.KeymapConfigError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
