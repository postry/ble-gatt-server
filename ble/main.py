"""
Nordic UART Service (NUS) BLE GATT server with a minimal UI.

- Windows: WinRT (``nus_winrt``) and packages in ``requirements.txt`` markers.
- Linux / macOS: Bless / BlueZ or Core Bluetooth (``nus_bless``).
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import traceback
from concurrent.futures import Future
from typing import Any

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from nus_common import DEFAULT_CHUNK_DELAY_MS, MAX_CHUNK_BYTES

if sys.platform == "win32":
    from nus_winrt import NusBleServerWinRt as NusBleServer
else:
    from nus_bless import NusBleServerBless as NusBleServer


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
        self._server: Any = NusBleServer(self._ble_loop, self._threadsafe_log)

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

            def done_stop(f: Future[None]) -> None:
                try:
                    f.result()
                except Exception as exc:  # noqa: BLE001
                    self._threadsafe_log(f"Stop failed: {exc}")

                def ui() -> None:
                    self._ble_running = False
                    self.toggle_btn.configure(text="Start BLE")

                self.root.after(0, ui)

            fut = asyncio.run_coroutine_threadsafe(self._server.stop_async(), self._ble_loop)
            fut.add_done_callback(lambda fobj: self.root.after(0, lambda: done_stop(fobj)))

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

        async def shutdown_async() -> None:
            await self._server.stop_async()
            self._ble_loop.stop()

        fut = asyncio.run_coroutine_threadsafe(shutdown_async(), self._ble_loop)
        try:
            fut.result(timeout=35.0)
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    MainWindow().run()


if __name__ == "__main__":
    main()
