"""Shared NUS constants and helpers for WinRT and Bless backends."""

from __future__ import annotations

import math
import struct
import uuid
from typing import Callable, Optional

NUS_SERVICE_UUID = uuid.UUID("6E400001-b5a3-f393-e0a9-e50e24dcca9e")
NUS_RX_UUID = uuid.UUID("6E400002-b5a3-f393-e0a9-e50e24dcca9e")
NUS_TX_UUID = uuid.UUID("6E400003-b5a3-f393-e0a9-e50e24dcca9e")

MAX_CHUNK_BYTES = 8
DEFAULT_CHUNK_DELAY_MS = 100

NUS_CHANNEL = 1
SINC_SAMPLE_COUNT = 256
SINC_X_MAX = 8.0 * math.pi

LogFn = Callable[[str], None]


def _sinc(x: float) -> float:
    if abs(x) < 1e-12:
        return 1.0
    return math.sin(x) / x


def _sinc_payloads() -> list[bytes]:
    out: list[bytes] = []
    for i in range(SINC_SAMPLE_COUNT):
        t = i / max(SINC_SAMPLE_COUNT - 1, 1)
        x = (2.0 * t - 1.0) * SINC_X_MAX
        y = _sinc(x)
        v = int(round(y * 32767.0))
        v = max(-32768, min(32767, v))
        out.append(struct.pack(">Hh", NUS_CHANNEL, v))
    return out


def _sinc_stream_bytes() -> bytes:
    return b"".join(_sinc_payloads())


def _chunk_preview(chunk: bytes) -> tuple[str, str]:
    hex_spaced = " ".join(f"{b:02x}" for b in chunk)
    ascii_safe = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    return hex_spaced, ascii_safe


def _rx_bytes_log_lines(
    raw: bytes,
    *,
    offset: int,
    write_option: Optional[str] = None,
) -> list[str]:
    opt_name = write_option if write_option is not None else "unknown"
    if not raw:
        return [f"RX [NUS]: len=0 offset={offset} option={opt_name} (empty payload)"]

    hex_spaced, ascii_safe = _chunk_preview(raw)
    lines = [
        f"RX [NUS]: len={len(raw)} offset={offset} option={opt_name}",
        f"RX [NUS]: hex=[{hex_spaced}]",
        f"RX [NUS]: ascii_preview=[{ascii_safe}]",
    ]
    try:
        text = raw.decode("utf-8")
        lines.append(f"RX [NUS]: utf8={text!r}")
    except UnicodeDecodeError:
        lines.append("RX [NUS]: utf8=(not valid UTF-8)")
    return lines
