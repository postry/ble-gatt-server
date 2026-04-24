"""
Nordic UART Service (NUS) BLE GATT server with a minimal UI — Python equivalent of
the referenced UWP App (GattServiceProvider + RX write / TX notify).

Requires Windows and the WinRT packages listed in requirements.txt.
"""

from __future__ import annotations

import asyncio
import sys
import threading
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

LogFn = Callable[[str], None]


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

    async def send(self, message: str) -> None:
        if not self._tx:
            return
        writer = DataWriter()
        writer.write_string(message)
        await self._tx.notify_value_async(writer.detach_buffer())


def _run_async_loop(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    asyncio.set_event_loop(loop)
    ready.set()
    loop.run_forever()


class MainWindow:
    def __init__(self) -> None:
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

        ttk.Label(frm, text="Log:").pack(anchor=tk.W)
        self.log_widget = scrolledtext.ScrolledText(frm, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.log_widget.pack(fill=tk.BOTH, expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _threadsafe_log(self, line: str) -> None:
        def append() -> None:
            self.log_widget.configure(state=tk.NORMAL)
            self.log_widget.insert(tk.END, line + "\n")
            self.log_widget.see(tk.END)
            self.log_widget.configure(state=tk.DISABLED)

        self.root.after(0, append)

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

    def _on_send(self) -> None:
        text = self.message_entry.get().strip()
        if not text:
            return

        async def _send() -> None:
            await self._server.send(text)

        asyncio.run_coroutine_threadsafe(_send(), self._ble_loop)

    def _on_close(self) -> None:
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
