"""Strict validation for approved Nillkin Cube Pocket firmware images."""

from dataclasses import dataclass
from enum import Enum
import hashlib
from types import MappingProxyType
from typing import Mapping

if __package__:
    from .phase2_analyze_fw import FirmwareAnalysisError, analyze_bytes
else:
    from phase2_analyze_fw import FirmwareAnalysisError, analyze_bytes


class ImageValidationError(RuntimeError):
    """The input is not one of the approved, internally valid images."""


class ImageKind(Enum):
    GLOBAL = "global"
    JP_LANG = "jp_lang"


@dataclass(frozen=True)
class FirmwareProfile:
    kind: ImageKind
    size: int
    sha256: str
    full_file_sum16: int
    embedded_version: bytes
    hardware_model: bytes
    keymap_marker_offset: int


@dataclass(frozen=True)
class ValidatedImage:
    profile: FirmwareProfile
    keymap: tuple[int, ...]


_GLOBAL_PROFILE = FirmwareProfile(
    kind=ImageKind.GLOBAL,
    size=123_916,
    sha256="00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f",
    full_file_sum16=0xEC27,
    embedded_version=b"B077T_US_13",
    hardware_model=b"PAR2801",
    keymap_marker_offset=0x1DAA0,
)

_JP_LANG_PROFILE = FirmwareProfile(
    kind=ImageKind.JP_LANG,
    size=123_916,
    sha256="3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246",
    full_file_sum16=0xEC29,
    embedded_version=b"B077T_US_13",
    hardware_model=b"PAR2801",
    keymap_marker_offset=0x1DAA0,
)

APPROVED_IMAGES: Mapping[ImageKind, FirmwareProfile] = MappingProxyType(
    {
        ImageKind.GLOBAL: _GLOBAL_PROFILE,
        ImageKind.JP_LANG: _JP_LANG_PROFILE,
    }
)


def _profile_for_digest(digest: str) -> FirmwareProfile:
    for profile in APPROVED_IMAGES.values():
        if profile.sha256 == digest:
            return profile
    raise ImageValidationError(f"unapproved firmware SHA-256: {digest}")


def validate_image(data: bytes) -> ValidatedImage:
    """Return parsed metadata only for an exact approved firmware image."""

    digest = hashlib.sha256(data).hexdigest()
    profile = _profile_for_digest(digest)

    if len(data) != profile.size:
        raise ImageValidationError(
            f"firmware size mismatch: expected {profile.size}, got {len(data)}"
        )
    checksum = sum(data) & 0xFFFF
    if checksum != profile.full_file_sum16:
        raise ImageValidationError(
            "firmware sum16 mismatch: "
            f"expected 0x{profile.full_file_sum16:04x}, got 0x{checksum:04x}"
        )

    try:
        analysis = analyze_bytes(data, source=profile.kind.value)
    except FirmwareAnalysisError as error:
        raise ImageValidationError(str(error)) from error

    expected_version = profile.embedded_version.decode("ascii")
    if analysis.model_version_strings != (expected_version,):
        raise ImageValidationError(
            f"embedded version {expected_version} must occur exactly once"
        )
    expected_model = profile.hardware_model.decode("ascii")
    if not analysis.hardware_model_strings or set(analysis.hardware_model_strings) != {
        expected_model
    }:
        raise ImageValidationError(
            f"hardware model strings must contain only {expected_model}"
        )
    if analysis.marker_offset != profile.keymap_marker_offset:
        raise ImageValidationError(
            "MJNK offset mismatch: "
            f"expected 0x{profile.keymap_marker_offset:x}, "
            f"got 0x{analysis.marker_offset:x}"
        )
    if len(analysis.keymap) != 130:
        raise ImageValidationError(
            f"keymap must contain exactly 130 entries; got {len(analysis.keymap)}"
        )
    return ValidatedImage(profile=profile, keymap=analysis.keymap)
