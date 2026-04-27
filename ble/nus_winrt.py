"""Nordic UART GATT server using Windows WinRT (peripheral)."""

from __future__ import annotations

import asyncio
import traceback
from typing import Optional

from winrt.windows.devices.bluetooth import (
    BluetoothConnectionStatus,
    BluetoothError,
    BluetoothLEDevice,
)
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties,
    GattLocalCharacteristicParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisingParameters,
    GattSession,
    GattWriteOption,
)
from winrt.windows.storage.streams import DataReader, DataWriter

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


class NusBleServerWinRt:
    """WinRT GATT server: NUS RX (write) and TX (notify)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, log: LogFn) -> None:
        self._loop = loop
        self._log = log
        self._provider: Optional[GattServiceProvider] = None
        self._tx = None
        self._tracked_le_device_ids: set[str] = set()
        self._le_devices: dict[str, BluetoothLEDevice] = {}

    @property
    def is_running(self) -> bool:
        return self._provider is not None

    def _schedule_rx(self, _sender, args) -> None:
        asyncio.run_coroutine_threadsafe(self._handle_rx_write(args), self._loop)

    def _schedule_le_connection_monitor(self, session: GattSession) -> None:
        try:
            dev_id = session.device_id
        except Exception as exc:  # noqa: BLE001
            self._log(f"BLE link monitor: no device_id on GattSession: {exc!r}")
            return
        asyncio.run_coroutine_threadsafe(self._track_le_device_connection_async(dev_id), self._loop)

    async def _track_le_device_connection_async(self, device_id) -> None:
        try:
            id_str = device_id.id
        except Exception as exc:  # noqa: BLE001
            self._log(f"BLE link monitor: invalid device_id: {exc!r}")
            return

        if id_str in self._tracked_le_device_ids:
            return

        try:
            le = await BluetoothLEDevice.from_id_async(id_str)
        except Exception as exc:  # noqa: BLE001
            self._log(f"BLE link monitor: BluetoothLEDevice.from_id_async failed id={id_str!r}: {exc!r}")
            return

        if le is None:
            self._log(f"BLE link monitor: BluetoothLEDevice.from_id_async returned null id={id_str!r}")
            return

        def on_connection_status_changed(device: BluetoothLEDevice, _o: object) -> None:
            try:
                did = device.device_id
                st = device.connection_status
            except Exception as exc:  # noqa: BLE001
                self._log(f"BLE link monitor: status callback error: {exc!r}")
                return
            try:
                name = device.name or ""
            except Exception:
                name = ""
            try:
                addr = int(device.bluetooth_address)
                addr_hex = f"{addr:#018x}"
            except Exception:
                addr_hex = "?"
            if st == BluetoothConnectionStatus.CONNECTED:
                self._log(
                    f"BLE connected: device_id={did!r} name={name!r} address={addr_hex} "
                    f"connection_status={st.name}"
                )
            elif st == BluetoothConnectionStatus.DISCONNECTED:
                self._log(
                    f"BLE disconnected: device_id={did!r} name={name!r} address={addr_hex} "
                    f"connection_status={st.name}"
                )
            else:
                self._log(f"BLE connection status: device_id={did!r} status={st!r}")

        try:
            le.add_connection_status_changed(on_connection_status_changed)
            self._tracked_le_device_ids.add(id_str)
            self._le_devices[id_str] = le
            cur = le.connection_status
            nm = le.name or ""
            addr = int(le.bluetooth_address)
            self._log(
                f"BLE link monitor started: device_id={id_str!r} name={nm!r} address={addr:#018x} "
                f"initial_connection_status={cur.name}"
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"BLE link monitor: add_connection_status_changed failed id={id_str!r}: {exc!r}")

    def _track_sessions_from_tx_subscribers(self) -> None:
        if not self._tx:
            return
        try:
            for client in self._tx.subscribed_clients:
                self._schedule_le_connection_monitor(client.session)
        except Exception as exc:  # noqa: BLE001
            self._log(f"TX subscribed_clients link monitor error: {exc!r}")

    async def _handle_rx_write(self, args) -> None:
        deferral = args.get_deferral()
        try:
            try:
                self._schedule_le_connection_monitor(args.session)
            except Exception as exc:  # noqa: BLE001
                self._log(f"RX: could not schedule BLE link monitor: {exc!r}")

            request = await args.get_request_async()
            offset = int(request.offset)
            write_opt = request.option

            reader = DataReader.from_buffer(request.value)
            n = int(reader.unconsumed_buffer_length)
            if n <= 0:
                raw = b""
            else:
                buf = bytearray(n)
                reader.read_bytes(buf)
                raw = bytes(buf)

            opt_label = write_opt.name if isinstance(write_opt, GattWriteOption) else repr(write_opt)
            for line in _rx_bytes_log_lines(raw, offset=offset, write_option=opt_label):
                self._log(line)
            request.respond()
        except Exception as exc:  # noqa: BLE001
            self._log(f"RX [NUS] error: {exc!r}\n{traceback.format_exc()}")
        finally:
            deferral.complete()

    def _on_tx_subscribed(self, _sender, _e) -> None:
        self._log("Client subscribed to TX")
        self._track_sessions_from_tx_subscribers()

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
        for le in list(self._le_devices.values()):
            try:
                le.close()
            except Exception:
                pass
        self._le_devices.clear()
        self._tracked_le_device_ids.clear()

    async def stop_async(self) -> None:
        self.stop()

    async def send_chunked(
        self,
        payload: bytes,
        *,
        stream_label: str,
        max_chunk: int = MAX_CHUNK_BYTES,
        delay_s: float,
        text_encoding: Optional[str] = None,
    ) -> None:
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
