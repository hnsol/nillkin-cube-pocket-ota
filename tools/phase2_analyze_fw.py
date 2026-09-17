#!/usr/bin/env python3
"""Analyze Nillkin Cube Pocket firmware images without modifying them."""

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import struct
import sys


class FirmwareAnalysisError(RuntimeError):
    """The input is not a safely recognizable firmware image."""


@dataclass(frozen=True)
class HidLocation:
    index: int
    offset: int


@dataclass(frozen=True)
class FirmwareAnalysis:
    source: str
    size: int
    sha256: str
    sum16: int
    model_version_strings: tuple[str, ...]
    hardware_model_strings: tuple[str, ...]
    firmware_revision_strings: tuple[str, ...]
    marker_offset: int
    keymap_offset: int
    keymap_range: tuple[int, int]
    keymap: tuple[int, ...]
    hid_usages: dict[int, tuple[HidLocation, ...]]


@dataclass(frozen=True)
class KeymapDifference:
    index: int
    global_offset: int
    korean_offset: int
    global_value: int
    korean_value: int


@dataclass(frozen=True)
class FirmwareComparison:
    size_delta: int
    keymap_differences: tuple[KeymapDifference, ...]


def _ascii_matches(data: bytes, pattern: bytes) -> tuple[str, ...]:
    return tuple(match.group().decode("ascii") for match in re.finditer(pattern, data))


def analyze_bytes(data: bytes, *, source: str) -> FirmwareAnalysis:
    marker_offsets = tuple(match.start() for match in re.finditer(rb"MJNK", data))
    if not marker_offsets:
        raise FirmwareAnalysisError(f"{source}: MJNK marker was not found")
    if len(marker_offsets) != 1:
        rendered = ", ".join(f"0x{offset:x}" for offset in marker_offsets)
        raise FirmwareAnalysisError(
            f"{source}: multiple MJNK markers found at {rendered}"
        )
    marker_offset = marker_offsets[0]
    keymap_offset = marker_offset + 4
    keymap_size = 130 * 2
    available = len(data) - keymap_offset
    if available < keymap_size:
        raise FirmwareAnalysisError(
            f"{source}: keymap table is truncated; expected {keymap_size} bytes, "
            f"available {max(available, 0)}"
        )
    keymap = struct.unpack_from("<130H", data, keymap_offset)
    tracked_usages = (0x04, 0x2C, 0x39, *range(0xE0, 0xE8))
    hid_usages = {
        usage: tuple(
            HidLocation(index, keymap_offset + index * 2)
            for index, value in enumerate(keymap)
            if value == usage
        )
        for usage in tracked_usages
    }
    return FirmwareAnalysis(
        source,
        len(data),
        hashlib.sha256(data).hexdigest(),
        sum(data) & 0xFFFF,
        _ascii_matches(data, rb"B077T_US_\d+"),
        _ascii_matches(data, rb"PAR2801"),
        _ascii_matches(data, rb"(?<![0-9.])1\.0\.0(?![0-9.])"),
        marker_offset,
        keymap_offset,
        (keymap_offset, keymap_offset + keymap_size),
        keymap,
        hid_usages,
    )


def analyze_file(path: str | Path) -> FirmwareAnalysis:
    path = Path(path)
    return analyze_bytes(path.read_bytes(), source=str(path))


def compare_firmware(
    global_firmware: FirmwareAnalysis, korean_firmware: FirmwareAnalysis
) -> FirmwareComparison:
    differences = tuple(
        KeymapDifference(
            index=index,
            global_offset=global_firmware.keymap_offset + index * 2,
            korean_offset=korean_firmware.keymap_offset + index * 2,
            global_value=global_value,
            korean_value=korean_value,
        )
        for index, (global_value, korean_value) in enumerate(
            zip(global_firmware.keymap, korean_firmware.keymap, strict=True)
        )
        if global_value != korean_value
    )
    return FirmwareComparison(
        size_delta=korean_firmware.size - global_firmware.size,
        keymap_differences=differences,
    )


def _format_strings(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "not found"


def _format_locations(locations: tuple[HidLocation, ...]) -> str:
    if not locations:
        return "not found"
    return "; ".join(
        f"index {location.index}, offset 0x{location.offset:x}"
        for location in locations
    )


def print_analysis(label: str, result: FirmwareAnalysis) -> None:
    print(label)
    print(f"  path: {result.source}")
    print(f"  size: {result.size} bytes")
    print(f"  sha256: {result.sha256}")
    print(f"  sum16: 0x{result.sum16:04x}")
    print(f"  model/version: {_format_strings(result.model_version_strings)}")
    print(f"  hardware model: {_format_strings(result.hardware_model_strings)}")
    print(f"  firmware revision: {_format_strings(result.firmware_revision_strings)}")
    print(f"  MJNK offset: 0x{result.marker_offset:x}")
    print(
        f"  keymap range: [0x{result.keymap_range[0]:x}, "
        f"0x{result.keymap_range[1]:x})"
    )
    print(f"  A (0x04): {_format_locations(result.hid_usages[0x04])}")
    print(f"  Space (0x2c): {_format_locations(result.hid_usages[0x2C])}")
    print(f"  Caps Lock (0x39): {_format_locations(result.hid_usages[0x39])}")
    for modifier in range(0xE0, 0xE8):
        print(
            f"  modifier 0x{modifier:02x}: "
            f"{_format_locations(result.hid_usages[modifier])}"
        )


def print_comparison(comparison: FirmwareComparison) -> None:
    print("COMPARISON")
    print(f"  file size delta (KR - GLOBAL): {comparison.size_delta:+d} bytes")
    print("  keymap differences:")
    if not comparison.keymap_differences:
        print("    none")
        return
    for difference in comparison.keymap_differences:
        print(
            f"    index {difference.index}: "
            f"GLOBAL 0x{difference.global_value:02x} @ "
            f"0x{difference.global_offset:x} -> "
            f"KR 0x{difference.korean_value:02x} @ "
            f"0x{difference.korean_offset:x}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only comparison of GLOBAL and KR Nillkin firmware"
    )
    parser.add_argument("global_firmware", type=Path)
    parser.add_argument("korean_firmware", type=Path)
    args = parser.parse_args(argv)

    global_result = analyze_file(args.global_firmware)
    korean_result = analyze_file(args.korean_firmware)
    print_analysis("GLOBAL", global_result)
    print_analysis("KR", korean_result)
    print_comparison(compare_firmware(global_result, korean_result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FirmwareAnalysisError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
