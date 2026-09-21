"""Async BLE controller for the CH9141 wheelchair chassis."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # The launcher reports a clearer installation message.
    BleakClient = None
    BleakScanner = None

from protocol import (
    FrameDecoder,
    HANDSHAKE,
    NOTIFY_UUID,
    SERVICE_UUID,
    WRITE_UUID,
    encode_joystick,
    mode_command,
)


@dataclass(frozen=True)
class ScanDevice:
    key: str
    name: str
    details: object
    preferred: bool


class WheelchairBLE:
    def __init__(
        self,
        emit: Callable[[str, Any], None],
        client_factory=None,
        scanner=None,
        exchange_timeout: float = 10.0,
        send_interval: float = 0.1,
    ):
        self._emit_callback = emit
        self._client_factory = client_factory or BleakClient
        self._scanner = scanner or BleakScanner
        self._exchange_timeout = exchange_timeout
        self._command_retry_interval = 0.5
        self._send_interval = send_interval
        self._client = None
        self._write_characteristic = None
        self._decoder = FrameDecoder()
        self._waiters: dict[str, asyncio.Future[str]] = {}
        self._sender_task: asyncio.Task[None] | None = None
        self._stick = (0.0, 0.0)
        self._held = False
        self._last_motion_frame: str | None = None
        self._motion_writes_since_report = 0
        self._last_status = None
        self.ready = False
        self.mode = 1

    def _emit(self, kind: str, payload: Any = None) -> None:
        self._emit_callback(kind, payload)

    def _set_status(self, status: str) -> None:
        if status != self._last_status:
            self._last_status = status
            self._emit("status", status)

    async def scan(self, timeout: float = 5.0) -> list[ScanDevice]:
        if self._scanner is None:
            raise RuntimeError("Bleak is not installed")
        self._set_status("scanning")
        try:
            discovered = await self._scanner.discover(timeout=timeout, return_adv=True)
        except BaseException:
            self._set_status("idle")
            raise
        devices = []
        for device, advertisement in discovered.values():
            name = advertisement.local_name or device.name
            if not name:
                continue
            advertised = {uuid.lower() for uuid in advertisement.service_uuids or []}
            devices.append(
                ScanDevice(
                    key=device.address,
                    name=name,
                    details=device,
                    preferred=SERVICE_UUID in advertised,
                )
            )
        devices.sort(key=lambda item: (not item.preferred, item.name.casefold()))
        self._emit("devices", devices)
        self._set_status("idle")
        return devices

    async def connect(self, device: ScanDevice) -> None:
        if self._client_factory is None:
            raise RuntimeError("Bleak is not installed")
        if self._client is not None:
            await self.disconnect()

        self._set_status("connecting")
        self._client = self._client_factory(
            device.details, disconnected_callback=self._on_disconnected
        )
        try:
            await self._client.connect()
            services = self._client.services
            if services.get_service(SERVICE_UUID) is None:
                raise RuntimeError("device does not provide CH9141 service FFF0")
            notify = services.get_characteristic(NOTIFY_UUID)
            self._write_characteristic = services.get_characteristic(WRITE_UUID)
            if notify is None or self._write_characteristic is None:
                raise RuntimeError("device is missing CH9141 FFF1/FFF2 characteristics")

            await self._client.start_notify(notify, self._on_notification)
            self._set_status("handshaking")
            for request, acknowledgement in HANDSHAKE:
                await self._exchange(request, acknowledgement)

            # The supplier app always performs the O3 connection handshake, but
            # its exposed control panel sends motion frames with profile 1.
            self.mode = 1
            self._held = False
            self._stick = (0.0, 0.0)
            self._last_motion_frame = None
            self._motion_writes_since_report = 0
            self.ready = True
            self._sender_task = asyncio.create_task(self._send_loop())
            self._set_status("ready")
        except BaseException:
            self.ready = False
            await self.disconnect(send_center=False)
            raise

    async def _exchange(
        self, request: str, acknowledgement: str, *, retry: bool = True
    ) -> None:
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._waiters[acknowledgement] = waiter
        self._waiters["Y1ES"] = waiter
        try:
            deadline = loop.time() + self._exchange_timeout
            while not waiter.done():
                await self._write(request)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        asyncio.shield(waiter),
                        timeout=(
                            min(self._command_retry_interval, remaining)
                            if retry
                            else remaining
                        ),
                    )
                except TimeoutError:
                    if not retry:
                        break
            if not waiter.done():
                raise TimeoutError(
                    f"发送 {request} 后等待 {acknowledgement} 超时"
                    f"（{self._exchange_timeout:g} 秒）"
                )
            if waiter.result() == "Y1ES":
                raise RuntimeError(
                    f"执行 {request} 时底盘返回 Y1ES（控制连接错误），请重新连接"
                )
        finally:
            for frame in (acknowledgement, "Y1ES"):
                if self._waiters.get(frame) is waiter:
                    self._waiters.pop(frame, None)

    def _on_notification(self, _characteristic, data: bytearray) -> None:
        for frame in self._decoder.feed(data):
            self._emit("rx", frame)
            waiter = self._waiters.get(frame)
            if waiter is not None and not waiter.done():
                waiter.set_result(frame)
            if frame == "Y1ES":
                if waiter is None:
                    self._emit(
                        "error", "底盘返回 Y1ES（控制连接错误），已停止发送并断开"
                    )
                self.ready = False
                self._held = False
                self._stick = (0.0, 0.0)
                asyncio.create_task(self.disconnect(send_center=False))

    def _on_disconnected(self, _client) -> None:
        self.ready = False
        self._held = False
        self._stick = (0.0, 0.0)
        self._set_status("disconnected")

    async def _write(self, frame: str) -> None:
        if self._client is None or self._write_characteristic is None:
            raise RuntimeError("wheelchair is not connected")
        await self._client.write_gatt_char(
            self._write_characteristic, frame.encode("ascii"), response=False
        )
        self._emit("tx", frame)

    async def _send_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            next_send = loop.time()
            while self.ready:
                x, y = self._stick if self._held else (0.0, 0.0)
                frame = encode_joystick(x, y, self.mode)
                if frame != self._last_motion_frame:
                    self._last_motion_frame = frame
                    self._motion_writes_since_report = 0
                    self._emit("motion", frame)
                else:
                    self._motion_writes_since_report += 1
                    if self._motion_writes_since_report >= 10:
                        self._motion_writes_since_report = 0
                        self._emit("motion", frame)
                await self._write(frame)
                next_send += self._send_interval
                delay = next_send - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_send = loop.time()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.ready = False
            self._held = False
            self._emit("error", f"motion write failed: {error}")
            await self.disconnect(send_center=True)

    def set_stick(self, x: float, y: float, held: bool) -> None:
        self._held = bool(held)
        self._stick = (float(x), float(y)) if self._held else (0.0, 0.0)

    async def set_mode(self, mode: int) -> None:
        if not self.ready:
            raise RuntimeError("wheelchair is not ready")
        request, acknowledgement = mode_command(mode)
        self._held = False
        self._stick = (0.0, 0.0)
        await self._stop_sender()
        try:
            await self._write("Y2WS")
            await self._exchange(request, acknowledgement, retry=False)
        except Exception:
            self._emit("mode", self.mode)
            raise
        else:
            self.mode = mode
            self._emit("mode", mode)
        finally:
            if self.ready and self._sender_task is None:
                self._sender_task = asyncio.create_task(self._send_loop())

    async def _stop_sender(self) -> None:
        task, self._sender_task = self._sender_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def emergency_stop(self) -> None:
        self.ready = False
        self._held = False
        self._stick = (0.0, 0.0)
        await self._stop_sender()
        try:
            if self._client is not None and self._write_characteristic is not None:
                center = encode_joystick(0.0, 0.0, self.mode)
                for index in range(3):
                    await self._write(center)
                    if index < 2:
                        await asyncio.sleep(0.05)
        finally:
            await self.disconnect(send_center=False)

    async def disconnect(self, send_center: bool = True) -> None:
        self.ready = False
        self._held = False
        self._stick = (0.0, 0.0)
        await self._stop_sender()
        client, self._client = self._client, None
        try:
            if (
                send_center
                and client is not None
                and client.is_connected
                and self._write_characteristic is not None
            ):
                await client.write_gatt_char(
                    self._write_characteristic,
                    encode_joystick(0.0, 0.0, self.mode).encode("ascii"),
                    response=False,
                )
        finally:
            self._write_characteristic = None
            for waiter in self._waiters.values():
                if not waiter.done():
                    waiter.cancel()
            self._waiters.clear()
            if client is not None and client.is_connected:
                await client.disconnect()
            self._set_status("disconnected")
