"""Transport-neutral PixArt OTA wire protocol and transfer planner."""

import struct
from collections.abc import Iterator
from dataclasses import dataclass

MAX_OBJECT_SIZE = 4096


class ProtocolError(ValueError):
    """Raised when an OTA value or frame is not safe to use."""


@dataclass(frozen=True)
class OtaState:
    status: int
    new_flow: int
    offset: int
    checksum: int
    max_object_size: int
    mtu_size: int
    prn_threshold: int
    spec_result: int


@dataclass(frozen=True)
class TransferOperation:
    kind: str
    payload: bytes = b""
    expected_opcode: int | None = None
    expected_checksum: int | None = None


def _require_uint(value: int, bits: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"{name} must be an unsigned {bits}-bit integer")
    if not 0 <= value < 1 << bits:
        raise ProtocolError(f"{name} is outside the unsigned {bits}-bit range")


def build_retransmit() -> bytes:
    return b"\x28\x00"


def build_legacy_init() -> bytes:
    return b"\x10\x00"


def build_init_new(fw_size: int) -> bytes:
    _require_uint(fw_size, 32, "firmware size")
    return b"\x27" + struct.pack("<I", fw_size) + b"\x00"


def build_object_create(address: int, size: int) -> bytes:
    _require_uint(address, 32, "object address")
    _require_uint(size, 32, "object size")
    if not 1 <= size <= MAX_OBJECT_SIZE:
        raise ProtocolError("object size must be between 1 and 4096 bytes")
    return b"\x25" + struct.pack("<II", address, size)


def build_upgrade(fw_size: int, checksum: int, version: str) -> bytes:
    _require_uint(fw_size, 32, "firmware size")
    _require_uint(checksum, 16, "checksum")
    try:
        encoded_version = version.encode("ascii")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise ProtocolError("version must be ASCII") from exc
    if len(encoded_version) > 10:
        raise ProtocolError("version must be at most 10 ASCII bytes")
    return (
        b"\x18"
        + struct.pack("<IH", fw_size, checksum)
        + encoded_version.ljust(10, b"\x00")
    )


def build_reset() -> bytes:
    return b"\x22\x00"


def parse_init_new_response(frame: bytes) -> OtaState:
    if len(frame) != 18:
        raise ProtocolError("init-new response must be exactly 18 bytes")
    if frame[:3] != b"\x0e\x10\x27":
        raise ProtocolError("init-new response envelope does not match")

    status = frame[3]
    new_flow = frame[4]
    offset, checksum, max_object_size, mtu_size, prn_threshold = struct.unpack_from(
        "<HHIHH", frame, 5
    )
    spec_result = frame[17]
    if status != 0:
        raise ProtocolError("init-new status is not successful")
    if new_flow != 1:
        raise ProtocolError("init-new response did not select the new flow")
    if spec_result != 1:
        raise ProtocolError("init-new specification result is not successful")
    if not 1 <= max_object_size <= MAX_OBJECT_SIZE:
        raise ProtocolError("device max object size is invalid")
    if mtu_size == 0:
        raise ProtocolError("device MTU size must be positive")
    if prn_threshold == 0:
        raise ProtocolError("device PRN threshold must be positive")

    return OtaState(
        status=status,
        new_flow=new_flow,
        offset=offset,
        checksum=checksum,
        max_object_size=max_object_size,
        mtu_size=mtu_size,
        prn_threshold=prn_threshold,
        spec_result=spec_result,
    )


def sum16(data: bytes) -> int:
    return sum(data) & 0xFFFF


def _validate_transfer_state(state: OtaState) -> None:
    _require_uint(state.status, 8, "OTA status")
    _require_uint(state.new_flow, 8, "OTA new-flow flag")
    _require_uint(state.offset, 16, "OTA resume offset")
    _require_uint(state.checksum, 16, "OTA resume checksum")
    _require_uint(state.max_object_size, 32, "device max object size")
    _require_uint(state.mtu_size, 16, "device MTU size")
    _require_uint(state.prn_threshold, 16, "device PRN threshold")
    _require_uint(state.spec_result, 8, "OTA specification result")
    if state.status != 0 or state.new_flow != 1 or state.spec_result != 1:
        raise ProtocolError("OTA state is not ready for the new transfer flow")
    if not 1 <= state.max_object_size <= MAX_OBJECT_SIZE:
        raise ProtocolError("device max object size is invalid")
    if state.mtu_size <= 0:
        raise ProtocolError("device MTU size must be positive")
    if state.prn_threshold <= 0:
        raise ProtocolError("device PRN threshold must be positive")


def iter_transfer_operations(
    data: bytes,
    state: OtaState,
    version: str,
    *,
    payload_chunk_size: int | None = None,
) -> Iterator[TransferOperation]:
    if not data:
        raise ProtocolError("firmware data must not be empty")
    _require_uint(len(data), 32, "firmware size")
    _validate_transfer_state(state)
    if payload_chunk_size is None:
        payload_chunk_size = state.mtu_size
    _require_uint(payload_chunk_size, 16, "payload chunk size")
    if payload_chunk_size <= 0:
        raise ProtocolError("payload chunk size must be positive")
    if payload_chunk_size > state.mtu_size:
        raise ProtocolError("payload chunk size exceeds device MTU size")
    upgrade_payload = build_upgrade(len(data), sum16(data), version)

    object_size = state.max_object_size
    object_count = (len(data) + object_size - 1) // object_size
    resume_offset = state.offset
    if not 0 <= resume_offset <= object_count:
        resume_offset = 0
    resume_end = min(resume_offset * object_size, len(data))
    if sum16(data[:resume_end]) != state.checksum:
        resume_offset = 0
        resume_end = 0

    running_checksum = sum16(data[:resume_end])
    for object_index in range(resume_offset, object_count):
        address = object_index * object_size
        object_data = data[address : address + object_size]
        yield TransferOperation(
            "object-create", build_object_create(address, object_size)
        )
        yield TransferOperation("wait-object", expected_opcode=0x25)

        fragments_since_ack = 0
        for chunk_start in range(0, len(object_data), payload_chunk_size):
            chunk = object_data[chunk_start : chunk_start + payload_chunk_size]
            running_checksum = (running_checksum + sum(chunk)) & 0xFFFF
            yield TransferOperation("payload", chunk)
            fragments_since_ack += 1
            object_ended = chunk_start + len(chunk) == len(object_data)
            if fragments_since_ack == state.prn_threshold or object_ended:
                yield TransferOperation(
                    "wait-prn",
                    expected_opcode=0x17,
                    expected_checksum=running_checksum,
                )
                fragments_since_ack = 0

    yield TransferOperation("upgrade", upgrade_payload)
    yield TransferOperation("wait-upgrade", expected_opcode=0x18)
    yield TransferOperation("reset", build_reset())
