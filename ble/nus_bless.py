"""Nordic UART GATT server using Bless (BlueZ on Linux, Core Bluetooth on macOS)."""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Optional
from uuid import UUID

from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

from nus_common import (
    MAX_CHUNK_BYTES,
    NUS_RX_UUID,
    NUS_SERVICE_UUID,
    NUS_TX_UUID,
    LogFn,
    _chunk_preview,
    _rx_bytes_log_lines,
    _sinc_stream_bytes,
)


def _uuid_key(u: UUID) -> str:
    return str(u)


class NusBleServerBless:
    """Bless / BlueZ GATT server: NUS RX (write) and TX (notify)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, log: LogFn) -> None:
        self._loop = loop
        self._log = log
        self._server: Optional[BlessServer] = None
        self._svc = _uuid_key(NUS_SERVICE_UUID)
        self._rx = _uuid_key(NUS_RX_UUID)
        self._tx = _uuid_key(NUS_TX_UUID)

    @property
    def is_running(self) -> bool:
        return self._server is not None

    def _bless_read(self, characteristic: Any) -> bytearray:
        if characteristic is None:
            return bytearray()
        val = getattr(characteristic, "value", None)
        if val is None:
            return bytearray()
        return bytearray(val)

    def _bless_write(self, characteristic: Any, value: Any) -> None:
        if characteristic is None:
            return
        try:
            cu = str(UUID(str(characteristic.uuid)))
        except Exception:  # noqa: BLE001
            return
        if cu != self._rx:
            return
        raw = bytes(value) if value else b""
        for line in _rx_bytes_log_lines(raw, offset=0, write_option="bless/BlueZ"):
            self._log(line)
        try:
            characteristic.value = bytearray(raw) if raw else bytearray()
        except Exception:  # noqa: BLE001
            pass

    async def start(self) -> bool:
        if self._server is not None:
            return True

        server = BlessServer(name="NUS-Python", loop=self._loop)
        await server.setup_task

        server.read_request_func = self._bless_read
        server.write_request_func = self._bless_write

        def _on_start_notify(_: Any) -> None:
            self._log("Client subscribed to TX")

        server.app.StartNotify = _on_start_notify

        gatt = {
            self._svc: {
                self._rx: {
                    "Properties": (
                        GATTCharacteristicProperties.write
                        | GATTCharacteristicProperties.write_without_response
                    ),
                    "Permissions": (
                        GATTAttributePermissions.readable | GATTAttributePermissions.writeable
                    ),
                    "Value": None,
                },
                self._tx: {
                    "Properties": GATTCharacteristicProperties.notify,
                    "Permissions": GATTAttributePermissions.readable,
                    "Value": bytearray(),
                },
            }
        }

        try:
            await server.add_gatt(gatt)
            ok = await server.start()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Bless start failed: {exc!r}\n{traceback.format_exc()}")
            try:
                await server.stop()
            except Exception:  # noqa: BLE001
                pass
            return False

        if not ok:
            self._log("Bless server.start() returned False.")
            try:
                await server.stop()
            except Exception:  # noqa: BLE001
                pass
            return False

        self._server = server
        self._log("Advertising started (NUS, Bless/BlueZ).")
        return True

    async def stop_async(self) -> None:
        srv = self._server
        self._server = None
        if srv is None:
            return
        try:
            await srv.stop()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Bless stop error: {exc!r}")
        self._log("Advertising stopped.")

    async def send_chunked(
        self,
        payload: bytes,
        *,
        stream_label: str,
        max_chunk: int = MAX_CHUNK_BYTES,
        delay_s: float,
        text_encoding: Optional[str] = None,
    ) -> None:
        if not self._server:
            self._log("TX skipped: no server (BLE not started?).")
            return
        if max_chunk < 1:
            self._log("TX skipped: max_chunk must be >= 1.")
            return
        if not payload:
            self._log(f"TX [{stream_label}]: empty payload, nothing sent.")
            return

        total = len(payload)
        n_chunks = (total + max_chunk - 1) // max_chunk
        delay_ms = delay_s * 1000.0
        self._log(
            f"TX [{stream_label}] start: total_bytes={total}, max_chunk_bytes={max_chunk}, "
            f"chunk_count={n_chunks}, inter_chunk_delay_ms={delay_ms:.3g}, "
            f"encoding={text_encoding or 'n/a (binary)'}"
        )

        tx_char = self._server.get_characteristic(self._tx)
        if tx_char is None:
            self._log("TX skipped: TX characteristic not found.")
            return

        try:
            for i in range(n_chunks):
                start = i * max_chunk
                end = min(start + max_chunk, total)
                chunk = payload[start:end]
                hex_spaced, ascii_safe = _chunk_preview(chunk)

                tx_char.value = bytearray(chunk)
                self._server.update_value(self._svc, self._tx)

                self._log(
                    f"TX [{stream_label}] chunk {i + 1}/{n_chunks}: "
                    f"bytes_offset=[{start}:{end}), chunk_len={len(chunk)}, "
                    f"hex=[{hex_spaced}], ascii_preview=[{ascii_safe}], notify=completed"
                )

                is_last = i >= n_chunks - 1
                if not is_last and delay_s > 0:
                    self._log(
                        f"TX [{stream_label}] chunk {i + 1}/{n_chunks}: "
                        f"inter_chunk_delay_ms={delay_ms:.3g} (sleep before chunk {i + 2})…"
                    )
                    await asyncio.sleep(delay_s)

            self._log(f"TX [{stream_label}] done: sent {n_chunks} notification(s).")
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"TX [{stream_label}] ABORTED after error: {exc!r}\n{traceback.format_exc()}"
            )
            raise

    async def send(self, message: str, *, delay_s: float) -> None:
        payload = message.encode("utf-8")
        await self.send_chunked(
            payload,
            stream_label="text_utf8",
            delay_s=delay_s,
            text_encoding="utf-8",
        )

    async def send_sinc_series(self, *, delay_s: float) -> None:
        await self.send_chunked(
            _sinc_stream_bytes(),
            stream_label="sinc_binary",
            delay_s=delay_s,
        )
