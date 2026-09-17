"""Pure protocol definitions for the known read-only OTA commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProtocolError(ValueError):
    """A device response does not match the requested command."""


class WriteMode(Enum):
    WITH_RESPONSE = "with-response"
    WITHOUT_RESPONSE = "without-response"


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
