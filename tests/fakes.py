"""Reusable scripted GATT fakes for read-only OTA tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class FakeCharacteristic:
    uuid: str
    properties: tuple[str, ...]


@dataclass(frozen=True)
class FakeService:
    uuid: str
    characteristics: tuple[FakeCharacteristic, ...]


@dataclass(frozen=True)
class GattStep:
    operation: str
    value: bytes = b""
    response: bool | None = None
    error: Exception | None = None
    delay: float = 0
    disconnect: bool = False


def expected_services() -> tuple[FakeService, ...]:
    base = "0000{}-0000-1000-8000-00805f9b34fb"
    return (
        FakeService(
            base.format("ff00"),
            (
                FakeCharacteristic(
                    base.format("ff01"),
                    ("read", "write", "write-without-response", "notify"),
                ),
                FakeCharacteristic(base.format("ff02"), ("read", "write")),
                FakeCharacteristic(base.format("ff03"), ("read", "write")),
            ),
        ),
        FakeService(
            base.format("180a"),
            (
                FakeCharacteristic(base.format("2a24"), ("read",)),
                FakeCharacteristic(base.format("2a26"), ("read",)),
            ),
        ),
    )


class ScriptedGattClient:
    """A strict fake that consumes one declared GATT operation at a time."""

    def __init__(
        self,
        device: Any,
        *,
        timeout: float,
        script: list[GattStep],
        services: tuple[FakeService, ...] | None = None,
    ) -> None:
        self.device = device
        self.timeout = timeout
        self.services = services or expected_services()
        self._script = list(script)
        self.events: list[tuple[Any, ...]] = []
        self.writes: list[tuple[bytes, bool]] = []
        self.is_connected = True

    async def __aenter__(self) -> Self:
        self.events.append(("connect", self.device))
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.events.append(("disconnect",))
        self.is_connected = False

    async def _consume(self, operation: str) -> GattStep:
        if not self._script:
            raise AssertionError(f"unexpected {operation}: script exhausted")
        step = self._script.pop(0)
        if step.operation != operation:
            raise AssertionError(f"expected {step.operation}, got {operation}")
        if step.delay:
            await asyncio.sleep(step.delay)
        if step.disconnect:
            self.is_connected = False
        if step.error is not None:
            raise step.error
        return step

    async def read_gatt_char(self, characteristic: Any) -> bytes:
        self.events.append(("read", characteristic.uuid))
        return (await self._consume("read")).value

    async def write_gatt_char(
        self, characteristic: Any, data: bytes, *, response: bool
    ) -> None:
        payload = bytes(data)
        self.events.append(("write", characteristic.uuid, payload, response))
        self.writes.append((payload, response))
        step = await self._consume("write")
        if step.value != payload:
            raise AssertionError(f"expected write {step.value!r}, got {payload!r}")
        if step.response is not response:
            raise AssertionError(
                f"expected response={step.response}, got response={response}"
            )

    def assert_complete(self) -> None:
        if self._script:
            raise AssertionError(f"unconsumed GATT steps: {self._script!r}")
