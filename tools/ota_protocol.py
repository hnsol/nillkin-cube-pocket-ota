"""Pure protocol definitions for the known read-only OTA commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProtocolError(ValueError):
    """A device response does not match the requested command."""


class WriteMode(Enum):
    WITH_RESPONSE = "with-response"
    WITHOUT_RESPONSE = "without-response"


class ModelIdentity(Enum):
    """Availability of the unverified vendor model query."""

    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CommandSpec:
    opcode: int
    request: bytes
    write_mode: WriteMode
    response_length: int


@dataclass(frozen=True)
class OtaFirmwareInfo:
    version: str
    checksum: int
    raw: bytes


READ_ONLY_COMMANDS = {
    0x10: CommandSpec(0x10, b"\x10\x00", WriteMode.WITH_RESPONSE, 4),
    0x23: CommandSpec(0x23, b"\x23\x00", WriteMode.WITH_RESPONSE, 11),
    0x2A: CommandSpec(0x2A, b"\x2a\x00", WriteMode.WITH_RESPONSE, 5),
    0x2B: CommandSpec(
        0x2B,
        b"\x2b\x00\x00\x00\x00",
        WriteMode.WITH_RESPONSE,
        25,
    ),
}


def validate_response(command: CommandSpec, frame: bytes) -> bytes:
    """Return a response after validating its vendor response envelope."""
    frame = bytes(frame)
    if (
        len(frame) != command.response_length
        or frame[0] != 0x0E
        or frame[1] != len(frame) - 2
        or frame[2] != command.opcode
        or frame[3] != 0x00
    ):
        raise ProtocolError(
            f"opcode 0x{command.opcode:02x} response does not match the protocol"
        )
    return frame


def parse_firmware_info(frame: bytes) -> OtaFirmwareInfo:
    """Parse the fields read by OTAUtility.UpdateFwInfo."""
    frame = validate_response(READ_ONLY_COMMANDS[0x23], frame)
    try:
        version = bytes(value for value in frame[4:9] if value).decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError("firmware version is not ASCII") from exc
    checksum = frame[9] | (frame[10] << 8)
    return OtaFirmwareInfo(version=version, checksum=checksum, raw=frame)


def parse_model_count(frame: bytes) -> int:
    """Parse a non-zero count from a validated GET_NUM_OF_MODEL response."""
    frame = validate_response(READ_ONLY_COMMANDS[0x2A], frame)
    count = frame[4]
    if count == 0:
        raise ProtocolError("vendor model count is zero")
    return count


def parse_model_info(frame: bytes) -> str:
    """Parse the NUL-terminated model identity used by the B077T gate."""
    frame = validate_response(READ_ONLY_COMMANDS[0x2B], frame)
    model_field = frame[6:18]
    if b"\x00" not in model_field:
        raise ProtocolError("vendor model identity is not NUL-terminated")
    raw_identity = model_field.split(b"\x00", 1)[0]
    try:
        identity = raw_identity.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError("vendor model identity is not ASCII") from exc
    if not identity or not identity.startswith("B077T"):
        raise ProtocolError("vendor model identity is not a supported B077T model")
    return identity
