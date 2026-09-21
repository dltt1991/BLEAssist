"""Wheelchair BLE serial protocol helpers."""

from __future__ import annotations

import math


SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"

HANDSHAKE = (("Y2AS", "Y1AS"), ("Y2BS", "Y1BS"), ("Y2O3S", "Y1O3S"))
CENTER_FRAME = "Y2C5005001S"


def _validated_mode(mode: int) -> int:
    if not isinstance(mode, int) or isinstance(mode, bool) or not 1 <= mode <= 6:
        raise ValueError("mode must be an integer from 1 to 6")
    return mode


def mode_command(mode: int) -> tuple[str, str]:
    """Return the mode request and its expected acknowledgement."""
    mode = _validated_mode(mode)
    return f"Y2O{mode}S", f"Y1O{mode}S"


def _clamp_unit(x: float, y: float) -> tuple[float, float]:
    magnitude = math.hypot(x, y)
    if magnitude > 1.0:
        return x / magnitude, y / magnitude
    return x, y


def encode_joystick(screen_x: float, screen_y: float, mode: int) -> str:
    """Encode normalized screen coordinates as the chassis joystick frame."""
    mode = _validated_mode(mode)
    screen_x, screen_y = _clamp_unit(float(screen_x), float(screen_y))
    protocol_x = math.floor(500 - screen_y * 100 + 0.5)
    protocol_y = math.floor(500 - screen_x * 100 + 0.5)
    return f"Y2C{protocol_x:03d}{protocol_y:03d}{mode}S"


def pointer_to_stick(
    pointer_x: float,
    pointer_y: float,
    center_x: float,
    center_y: float,
    radius: float,
) -> tuple[float, float]:
    """Map canvas pointer coordinates to a normalized circular stick."""
    if radius <= 0:
        raise ValueError("radius must be positive")
    return _clamp_unit(
        (pointer_x - center_x) / radius,
        (pointer_y - center_y) / radius,
    )


class FrameDecoder:
    """Split a notification byte stream into ASCII frames from Y through S."""

    def __init__(self, max_buffer: int = 256):
        if max_buffer < 2:
            raise ValueError("max_buffer must be at least 2")
        self._buffer = bytearray()
        self._max_buffer = max_buffer

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes | bytearray) -> list[str]:
        self._buffer.extend(data)
        frames: list[str] = []

        while self._buffer:
            start = self._buffer.find(b"Y")
            if start < 0:
                self._buffer.clear()
                break
            if start:
                del self._buffer[:start]

            end = self._buffer.find(b"S", 1)
            if end < 0:
                if len(self._buffer) > self._max_buffer:
                    tail = self._buffer[-self._max_buffer :]
                    last_start = tail.rfind(b"Y")
                    self._buffer = tail[last_start:] if last_start >= 0 else bytearray()
                break

            raw = bytes(self._buffer[: end + 1])
            del self._buffer[: end + 1]
            try:
                frames.append(raw.decode("ascii"))
            except UnicodeDecodeError:
                continue

        return frames
