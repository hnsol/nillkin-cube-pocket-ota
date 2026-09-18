"""Strict parser for declarative Nillkin keymap remap files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib


class KeymapConfigError(ValueError):
    """The remap configuration is malformed or unsafe."""


# These names represent physical keys whose original firmware entries are known.
PHYSICAL_KEYS = frozenset(
    {
        "caps_lock",
        "left_control",
        "left_alt",
        "left_gui",
        "right_gui",
        "right_alt",
    }
)

# HID Usage IDs which the verified GLOBAL keymap stores in one-byte entries.
HID_USAGES = {
    "caps_lock": 0x39,
    "space": 0x2C,
    "left_control": 0xE0,
    "left_shift": 0xE1,
    "left_alt": 0xE2,
    "left_gui": 0xE3,
    "right_control": 0xE4,
    "right_shift": 0xE5,
    "right_alt": 0xE6,
    "right_gui": 0xE7,
    "lang1": 0x90,
    "lang2": 0x91,
}


@dataclass(frozen=True)
class KeymapConfig:
    """Validated physical-key to HID Usage remapping."""

    remap: dict[str, int]


def _raise_toml_error(error: tomllib.TOMLDecodeError) -> KeymapConfigError:
    message = str(error)
    if "Cannot overwrite a value" in message or "duplicate" in message.lower():
        return KeymapConfigError(f"duplicate TOML key: {message}")
    return KeymapConfigError(f"invalid TOML: {message}")


def load_config(path: str | Path) -> KeymapConfig:
    """Load exactly format_version=1 and a non-empty symbolic [remap] table."""

    path = Path(path)
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise KeymapConfigError(f"cannot read config {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise _raise_toml_error(error) from error

    if parsed.get("format_version") != 1:
        raise KeymapConfigError("format_version must be 1")
    if set(parsed) != {"format_version", "remap"}:
        raise KeymapConfigError("only format_version and [remap] are allowed")
    remap = parsed.get("remap")
    if not isinstance(remap, dict) or not remap:
        raise KeymapConfigError("[remap] must be non-empty")

    resolved: dict[str, int] = {}
    for physical_key, usage_name in remap.items():
        if physical_key not in PHYSICAL_KEYS:
            raise KeymapConfigError(f"unknown physical key: {physical_key}")
        if not isinstance(usage_name, str) or usage_name not in HID_USAGES:
            raise KeymapConfigError(f"unknown HID usage: {usage_name!r}")
        resolved[physical_key] = HID_USAGES[usage_name]
    return KeymapConfig(remap=resolved)
