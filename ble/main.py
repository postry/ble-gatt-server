"""
Nordic UART Service (NUS) BLE GATT server with a minimal UI — Python equivalent of
the referenced UWP App (GattServiceProvider + RX write / TX notify).

Requires Windows and the WinRT packages listed in requirements.txt
(including winrt-Windows.Foundation.Collections for TX notify results).
"""

from __future__ import annotations

import asyncio
import math
import queue
import struct
import sys
import threading
import traceback
import uuid
from concurrent.futures import Future
from typing import Callable, Optional

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from winrt.windows.devices.bluetooth import BluetoothError
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties,
    GattLocalCharacteristicParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisingParameters,
)
from winrt.windows.storage.streams import DataReader, DataWriter

NUS_SERVICE_UUID = uuid.UUID("6E400001-b5a3-f393-e0a9-e50e24dcca9e")
NUS_RX_UUID = uuid.UUID("6E400002-b5a3-f393-e0a9-e50e24dcca9e")
NUS_TX_UUID = uuid.UUID("6E400003-b5a3-f393-e0a9-e50e24dcca9e")

MAX_CHUNK_BYTES = 8
DEFAULT_CHUNK_DELAY_MS = 100

# sin(x)/x stream: channel 1 (uint16 BE) + value (int16 BE) per sample → e.g. [0x00,0x01,0x12,0x34]
NUS_CHANNEL = 1
SINC_SAMPLE_COUNT = 256
SINC_X_MAX = 8.0 * math.pi

LogFn = Callable[[str], None]


def _sinc(x: float) -> float:
    if abs(x) < 1e-12:
        return 1.0
    return math.sin(x) / x


def _sinc_payloads() -> list[bytes]:
    """One 4-byte frame per x sample: >Hh = channel (1), int16 sinc scaled to ~full range."""
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


def _chunk_preview(chunk: bytes) -> str:
    hex_spaced = " ".join(f"{b:02x}" for b in chunk)
    ascii_safe = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    return hex_spaced, ascii_safe


class NusBleServer:
    """WinRT GATT server: NUS RX (write) and TX (notify), same UUIDs as the C# sample."""

    def __init__(self, loop: asyncio.AbstractEventLoop, log: LogFn) -> None:
        self._loop = loop
        self._log = log
        self._provider: Optional[GattServiceProvider] = None
        self._tx = None

    @property
    def is_running(self) -> bool:
        return self._provider is not None

    def _schedule_rx(self, _sender, args) -> None:
        asyncio.run_coroutine_threadsafe(self._handle_rx_write(args), self._loop)

    async def _handle_rx_write(self, args) -> None:
        deferral = args.get_deferral()
        try:
            request = await args.get_request_async()
            reader = DataReader.from_buffer(request.value)
            n = int(reader.unconsumed_buffer_length)
            received = reader.read_string(n) if n else ""
            self._log(f"RX: {received}")
            request.respond()
        except Exception as exc:  # noqa: BLE001 — surface to log; still complete deferral
            self._log(f"RX error: {exc}")
        finally:
            deferral.complete()

    def _on_tx_subscribed(self, _sender, _e) -> None:
        self._log("Client subscribed to TX")

    async def start(self) -> bool:
        if self._provider is not None:
            return True

        service_result = await GattServiceProvider.create_async(NUS_SERVICE_UUID)
        if service_result.error != BluetoothError.SUCCESS:
            self._log(f"Create service failed: {service_result.error!r}")
            return False

        self._provider = service_result.service_provider

        rx_params = GattLocalCharacteristicParameters()
        rx_params.characteristic_properties = (
            GattCharacteristicProperties.WRITE
            | GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
        )
        rx_params.write_protection_level = GattProtectionLevel.PLAIN

        rx_result = await self._provider.service.create_characteristic_async(NUS_RX_UUID, rx_params)
        if rx_result.error != BluetoothError.SUCCESS:
            self._log(f"Create RX failed: {rx_result.error!r}")
            self.stop()
            return False

        rx_char = rx_result.characteristic
        rx_char.add_write_requested(self._schedule_rx)

        tx_params = GattLocalCharacteristicParameters()
        tx_params.characteristic_properties = GattCharacteristicProperties.NOTIFY
        tx_params.read_protection_level = GattProtectionLevel.PLAIN
        tx_params.user_description = "NUS TX"

        tx_result = await self._provider.service.create_characteristic_async(NUS_TX_UUID, tx_params)
        if tx_result.error != BluetoothError.SUCCESS:
            self._log(f"Create TX failed: {tx_result.error!r}")
            self.stop()
            return False

        self._tx = tx_result.characteristic
        self._tx.add_subscribed_clients_changed(self._on_tx_subscribed)

        adv = GattServiceProviderAdvertisingParameters()
        adv.is_discoverable = True
        adv.is_connectable = True
        self._provider.start_advertising_with_parameters(adv)
        self._log("Advertising started (NUS).")
        return True

    def stop(self) -> None:
        if self._provider is not None:
            self._provider.stop_advertising()
            self._log("Advertising stopped.")
        self._provider = None
        self._tx = None

    async def send_chunked(
        self,
        payload: bytes,
        *,
        stream_label: str,
        max_chunk: int = MAX_CHUNK_BYTES,
        delay_s: float,
        text_encoding: Optional[str] = None,
    ) -> None:
        """Split payload into BLE notifications (max ``max_chunk`` bytes each), log each, ``asyncio.sleep`` between."""
        if not self._tx:
            self._log("TX skipped: no TX characteristic (BLE not started?).")
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

        try:
            for i in range(n_chunks):
                start = i * max_chunk
                end = min(start + max_chunk, total)
                chunk = payload[start:end]
                hex_spaced, ascii_safe = _chunk_preview(chunk)

                writer = DataWriter()
                writer.write_bytes(chunk)
                await self._tx.notify_value_async(writer.detach_buffer())

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
        """Send UTF-8–encoded text in chunks (max ``MAX_CHUNK_BYTES``), with delay between chunks."""
        payload = message.encode("utf-8")
        await self.send_chunked(
            payload,
            stream_label="text_utf8",
            delay_s=delay_s,
            text_encoding="utf-8",
        )

    async def send_sinc_series(self, *, delay_s: float) -> None:
        """Send concatenated sin(x)/x frames (4 bytes each) in chunks of ``MAX_CHUNK_BYTES``."""
        await self.send_chunked(
            _sinc_stream_bytes(),
            stream_label="sinc_binary",
            delay_s=delay_s,
        )


def _run_async_loop(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    asyncio.set_event_loop(loop)
    ready.set()
    loop.run_forever()


class MainWindow:
    def __init__(self) -> None:
        self._closing = False
        self._log_q: queue.Queue[str] = queue.Queue()
        self._ble_loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=_run_async_loop,
            args=(self._ble_loop, self._ready),
            name="ble-asyncio",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=30.0)

        self.root = tk.Tk()
        self.root.title("NUS BLE Server (Python)")
        self.root.minsize(420, 320)

        self._ble_running = False
        self._server = NusBleServer(self._ble_loop, self._threadsafe_log)

        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        self.toggle_btn = ttk.Button(frm, text="Start BLE", command=self._on_toggle_ble)
        self.toggle_btn.pack(anchor=tk.W, pady=(0, 8))

        msg_row = ttk.Frame(frm)
        msg_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(msg_row, text="Message:").pack(side=tk.LEFT)
        self.message_entry = ttk.Entry(msg_row)
        self.message_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        self.send_btn = ttk.Button(msg_row, text="Send", command=self._on_send)
        self.send_btn.pack(side=tk.RIGHT)

        sinc_row = ttk.Frame(frm)
        sinc_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            sinc_row,
            text="Send sin(x)/x binary stream (ch=1 + int16 each sample), chunked like text.",
        ).pack(side=tk.LEFT)
        self.send_sinc_btn = ttk.Button(sinc_row, text="Send sin(x)/x", command=self._on_send_sinc)
        self.send_sinc_btn.pack(side=tk.RIGHT)

        cfg_row = ttk.Frame(frm)
        cfg_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(cfg_row, text="Delay between TX chunks (ms):").pack(side=tk.LEFT)
        self.chunk_delay_ms_var = tk.StringVar(value=str(DEFAULT_CHUNK_DELAY_MS))
        self.chunk_delay_spin = ttk.Spinbox(
            cfg_row,
            from_=0,
            to=60_000,
            width=8,
            textvariable=self.chunk_delay_ms_var,
        )
        self.chunk_delay_spin.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(cfg_row, text=f"(max {MAX_CHUNK_BYTES} bytes per notify)").pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(frm, text="Log:").pack(anchor=tk.W)
        self.log_widget = scrolledtext.ScrolledText(frm, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.log_widget.pack(fill=tk.BOTH, expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_log_queue()

    def _append_log_direct(self, line: str) -> None:
        """Append to log widget; call only from the Tk main thread."""
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert(tk.END, line + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state=tk.DISABLED)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                line = self._log_q.get_nowait()
                self._append_log_direct(line)
        except queue.Empty:
            pass
        if self._closing:
            return
        try:
            if self.root.winfo_exists():
                self.root.after(30, self._poll_log_queue)
        except tk.TclError:
            pass

    def _threadsafe_log(self, line: str) -> None:
        """Safe from any thread (BLE/asyncio worker uses a queue; Tk drains on main thread)."""
        self._log_q.put(line)

    def _chunk_delay_seconds(self) -> float:
        raw = self.chunk_delay_ms_var.get().strip()
        try:
            ms = float(raw)
        except ValueError:
            ms = float(DEFAULT_CHUNK_DELAY_MS)
        if ms < 0:
            ms = 0.0
        return ms / 1000.0

    def _on_toggle_ble(self) -> None:
        if not self._ble_running:

            def done(fut: Future[bool]) -> None:
                try:
                    ok = fut.result()
                except Exception as exc:  # noqa: BLE001
                    self._threadsafe_log(f"Start failed: {exc}")
                    ok = False

                def ui() -> None:
                    if ok:
                        self._ble_running = True
                        self.toggle_btn.configure(text="Stop BLE")
                    else:
                        messagebox.showerror("BLE", "Could not start GATT server. See log.")

                self.root.after(0, ui)

            fut = asyncio.run_coroutine_threadsafe(self._server.start(), self._ble_loop)
            fut.add_done_callback(lambda f: self.root.after(0, lambda: done(f)))
        else:
            self._ble_loop.call_soon_threadsafe(self._server.stop)
            self._ble_running = False
            self.toggle_btn.configure(text="Start BLE")

    def _log_future_if_failed(self, label: str, fut: Future) -> None:
        try:
            fut.result()
        except Exception:
            self._threadsafe_log(f"{label} failed:\n{traceback.format_exc()}")

    def _on_send(self) -> None:
        text = self.message_entry.get().strip()
        if not text:
            return

        delay_s = self._chunk_delay_seconds()

        async def _send() -> None:
            await self._server.send(text, delay_s=delay_s)

        fut = asyncio.run_coroutine_threadsafe(_send(), self._ble_loop)
        fut.add_done_callback(lambda f: self._log_future_if_failed("Text send", f))

    def _on_send_sinc(self) -> None:
        if not self._server.is_running:
            messagebox.showinfo("BLE", "Start BLE and wait for a client before sending.")
            return

        delay_s = self._chunk_delay_seconds()

        async def _send_sinc() -> None:
            await self._server.send_sinc_series(delay_s=delay_s)

        fut = asyncio.run_coroutine_threadsafe(_send_sinc(), self._ble_loop)
        fut.add_done_callback(lambda f: self._log_future_if_failed("Sinc send", f))

    def _on_close(self) -> None:
        self._closing = True

        def shutdown() -> None:
            self._server.stop()
            self._ble_loop.stop()

        self._ble_loop.call_soon_threadsafe(shutdown)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if sys.platform != "win32":
        print("This application uses Windows WinRT BLE APIs and only runs on Windows.")
        sys.exit(1)
    MainWindow().run()


if __name__ == "__main__":
    main()
