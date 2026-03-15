"""
services/beckhoff_service.py — Beckhoff TwinCAT ADS communication service.

Uses the pyads library to exchange data with a Beckhoff TwinCAT PLC via ADS
(Automation Device Specification) over TCP port 48898 (AMS/TCP).

Data layout — uses the same 16-byte structure as PLCService/ProfinetService
mapped to a TwinCAT GVL (Global Variable List) or a DUT (Data Unit Type):

  TYPE ST_QC_Interface :
  STRUCT
      bFlags          : BYTE;       (* Offset 0  — see bit layout below *)
      bPadding        : BYTE;       (* Offset 1  — word-alignment pad   *)
      nDetectedCount  : INT;        (* Offset 2  — detected items       *)
      nExpectedCount  : INT;        (* Offset 4  — expected items       *)
      nCameraId       : INT;        (* Offset 6  — camera that produced result *)
      nDefectCount    : INT;        (* Offset 8  — running MISSING tally *)
      nOkCount        : INT;        (* Offset 10 — running OK tally      *)
      nBatchIdHash    : DINT;       (* Offset 12 — CRC-32 of batch ID    *)
  END_STRUCT
  END_TYPE

Bit layout for bFlags (offset 0):
  Bit 0 : trigger        (PLC → PC, rising edge → capture all cameras)
  Bit 1 : batch_active   (PC → PLC)
  Bit 2 : result_ok      (PC → PLC)
  Bit 3 : result_defect  (PC → PLC)
  Bit 4 : heartbeat      (PC → PLC, toggles every second)
  Bit 5 : system_ready   (PC → PLC)
  Bit 6 : ack_trigger    (PC → PLC, high while processing trigger)
  Bit 7 : inhibit        (PLC → PC, E-Stop / safety interlock)

ADS symbol access
-----------------
The service reads/writes via an ADS symbol handle.  Configure the symbol
name in settings.json ``beckhoff.symbol_name`` (default: ``GVL.stQC``).

TwinCAT project configuration
------------------------------
1. Create the struct type ST_QC_Interface in a GVL.
2. Declare an instance: ``stQC : ST_QC_Interface;``
3. Enable ADS on the target runtime (System Manager → Routes).
4. Add an ADS route from the PC to the TwinCAT runtime.
5. Set the AMS Net ID and port in settings.json.

Install::

    pip install pyads

If pyads is not installed BeckhoffService logs a warning and exits —
the rest of the application continues unaffected.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
import zlib
from typing import Optional

from PySide6.QtCore import QThread, Signal

import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional pyads import — graceful degradation when not installed
# ---------------------------------------------------------------------------
try:
    import pyads
    _PYADS_AVAILABLE = True
except ImportError:
    _PYADS_AVAILABLE = False
    logger.warning(
        "pyads is not installed — BeckhoffService will be disabled.  "
        "Install with:  pip install pyads"
    )

# ---------------------------------------------------------------------------
# DB byte / bit offset constants (same as PLCService for compatibility)
# ---------------------------------------------------------------------------
_DB_SIZE = 16

_BIT_TRIGGER       = 0
_BIT_BATCH_ACTIVE  = 1
_BIT_RESULT_OK     = 2
_BIT_RESULT_DEFECT = 3
_BIT_HEARTBEAT     = 4
_BIT_SYSTEM_READY  = 5
_BIT_ACK_TRIGGER   = 6
_BIT_INHIBIT       = 7

_OFFSET_DETECTED  = 2
_OFFSET_EXPECTED  = 4
_OFFSET_CAMERA_ID = 6
_OFFSET_DEFECT_CT = 8
_OFFSET_OK_CT     = 10
_OFFSET_HASH      = 12


class BeckhoffService(QThread):
    """
    Background QThread that communicates with a Beckhoff TwinCAT PLC via ADS.

    Drop-in replacement for PLCService / ProfinetService:
      - same signals: trigger_received, plc_connected, plc_disconnected, plc_error
      - same write methods: write_result, write_batch_state, write_system_ready, write_ack_clear

    The thread connects via ADS (TCP 48898), obtains a symbol handle for the
    configured struct, then enters a tight poll loop:

    1.  Read the full 16-byte struct from the PLC.
    2.  Detect a rising edge on bit 0.0 (trigger written by the PLC).
    3.  Toggle the heartbeat bit once per second.
    4.  Write the PC-owned output bytes back to the PLC.
    """

    trigger_received = Signal(int)
    plc_connected    = Signal()
    plc_disconnected = Signal()
    plc_error        = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._stop_event = threading.Event()
        self._lock       = threading.Lock()

        # Shadow output state
        self._flag_byte:    int = 0x00
        self._detected:     int = 0
        self._expected:     int = settings.EXPECTED_COUNT
        self._camera_id:    int = -1
        self._defect_count: int = 0
        self._ok_count:     int = 0
        self._batch_hash:   int = 0
        self._batch_id_cache: str = ""

        # Internal state
        self._last_trigger:       bool  = False
        self._heartbeat_state:    bool  = False
        self._last_heartbeat_flip: float = 0.0

    # =========================================================================
    # Public write API (called from MainWindow on the UI thread)
    # =========================================================================

    def write_result(
        self,
        camera_id: int,
        status: str,
        detected: int,
        expected: int,
    ) -> None:
        with self._lock:
            self._camera_id = _clamp_int(camera_id)
            self._detected  = _clamp_int(detected)
            self._expected  = _clamp_int(expected)
            self._set_flag(_BIT_RESULT_OK,     status == "OK")
            self._set_flag(_BIT_RESULT_DEFECT, status != "OK")

    def write_batch_state(
        self,
        active:       bool,
        ok_count:     int = 0,
        defect_count: int = 0,
        batch_id:     str = "",
    ) -> None:
        with self._lock:
            self._set_flag(_BIT_BATCH_ACTIVE, active)
            self._ok_count     = _clamp_int(ok_count)
            self._defect_count = _clamp_int(defect_count)
            if batch_id != self._batch_id_cache:
                self._batch_id_cache = batch_id
                self._batch_hash = zlib.crc32(batch_id.encode()) if batch_id else 0
            if not active:
                self._set_flag(_BIT_RESULT_OK,     False)
                self._set_flag(_BIT_RESULT_DEFECT, False)
                self._detected  = 0
                self._camera_id = -1

    def write_system_ready(self, ready: bool) -> None:
        with self._lock:
            self._set_flag(_BIT_SYSTEM_READY, ready)

    def write_ack_clear(self) -> None:
        with self._lock:
            self._set_flag(_BIT_ACK_TRIGGER, False)

    # =========================================================================
    # QThread entry point
    # =========================================================================

    def run(self) -> None:
        if not _PYADS_AVAILABLE:
            logger.warning(
                "BeckhoffService: pyads not available — thread exiting.  "
                "Install with: pip install pyads"
            )
            return

        ams_net_id    = settings.BECKHOFF_AMS_NET_ID
        ams_port      = settings.BECKHOFF_AMS_PORT
        symbol_name   = settings.BECKHOFF_SYMBOL_NAME
        poll_interval = settings.BECKHOFF_POLL_INTERVAL_MS / 1000.0
        reconnect     = settings.BECKHOFF_RECONNECT_DELAY
        reconnect_max = settings.BECKHOFF_RECONNECT_MAX

        logger.info(
            "BeckhoffService starting | ams=%s:%d symbol=%s poll=%dms",
            ams_net_id, ams_port, symbol_name, settings.BECKHOFF_POLL_INTERVAL_MS,
        )

        plc = None
        connected = False
        handle = None

        while not self._stop_event.is_set():

            # ------------------------------------------------------------------
            # Connection phase
            # ------------------------------------------------------------------
            if not connected:
                try:
                    plc = pyads.Connection(ams_net_id, ams_port)
                    plc.open()
                    handle = plc.get_handle(symbol_name)
                    connected = True
                    reconnect = settings.BECKHOFF_RECONNECT_DELAY
                    logger.info(
                        "Beckhoff ADS connected | ams=%s:%d symbol=%s",
                        ams_net_id, ams_port, symbol_name,
                    )
                    self.plc_connected.emit()
                except Exception as exc:
                    msg = f"Beckhoff ADS connect failed ({ams_net_id}:{ams_port}): {exc}"
                    logger.warning("%s — retrying in %.1f s", msg, reconnect)
                    self.plc_error.emit(msg)
                    if plc is not None:
                        try:
                            plc.close()
                        except Exception:
                            pass
                    plc = None
                    self._interruptible_sleep(reconnect)
                    reconnect = min(reconnect * 2.0, reconnect_max)
                    continue

            # ------------------------------------------------------------------
            # Poll phase
            # ------------------------------------------------------------------
            try:
                raw = plc.read_by_name(
                    symbol_name,
                    pyads.PLCTYPE_BYTE * _DB_SIZE,
                )
                raw = bytearray(raw)

                # Detect rising edge on trigger
                trigger_now = _get_bit(raw[0], _BIT_TRIGGER)
                inhibit_now = _get_bit(raw[0], _BIT_INHIBIT)
                if trigger_now and not self._last_trigger:
                    if inhibit_now:
                        logger.warning(
                            "Beckhoff trigger received but inhibit bit set — suppressed"
                        )
                    else:
                        logger.info("Beckhoff trigger rising edge — emitting trigger_received")
                        with self._lock:
                            self._set_flag(_BIT_ACK_TRIGGER, True)
                        self.trigger_received.emit(-1)
                self._last_trigger = trigger_now

                # Toggle heartbeat
                now = time.monotonic()
                if now - self._last_heartbeat_flip >= 1.0:
                    self._heartbeat_state     = not self._heartbeat_state
                    self._last_heartbeat_flip = now
                    with self._lock:
                        self._set_flag(_BIT_HEARTBEAT, self._heartbeat_state)

                # Write back
                with self._lock:
                    buf = self._build_write_buffer()
                buf[0] = (buf[0] & ~(1 << _BIT_TRIGGER)) | (raw[0] & (1 << _BIT_TRIGGER))

                plc.write_by_name(
                    symbol_name,
                    bytes(buf),
                    pyads.PLCTYPE_BYTE * _DB_SIZE,
                )

            except Exception as exc:
                msg = f"Beckhoff ADS communication error: {exc}"
                logger.warning("%s — reconnecting", msg)
                self.plc_error.emit(msg)
                try:
                    if handle is not None:
                        plc.release_handle(handle)
                except Exception:
                    pass
                try:
                    plc.close()
                except Exception:
                    pass
                handle = None
                plc = None
                connected = False
                self.plc_disconnected.emit()
                self._interruptible_sleep(reconnect)
                reconnect = min(reconnect * 2.0, reconnect_max)
                continue

            self._interruptible_sleep(poll_interval)

        # ------------------------------------------------------------------
        # Shutdown
        # ------------------------------------------------------------------
        if connected and plc is not None:
            try:
                with self._lock:
                    self._flag_byte = 0x00
                    buf = self._build_write_buffer()
                plc.write_by_name(
                    symbol_name,
                    bytes(buf),
                    pyads.PLCTYPE_BYTE * _DB_SIZE,
                )
            except Exception:
                pass
            try:
                if handle is not None:
                    plc.release_handle(handle)
            except Exception:
                pass
            try:
                plc.close()
            except Exception:
                pass

        logger.info("BeckhoffService stopped")

    def stop(self) -> None:
        self._stop_event.set()
        if not self.wait(5000):
            logger.warning("BeckhoffService did not stop cleanly — terminating")
            self.terminate()

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _set_flag(self, bit: int, value: bool) -> None:
        if value:
            self._flag_byte |=  (1 << bit)
        else:
            self._flag_byte &= ~(1 << bit)

    def _build_write_buffer(self) -> bytearray:
        buf = bytearray(_DB_SIZE)
        buf[0] = self._flag_byte & 0xFF
        struct.pack_into(">h", buf, _OFFSET_DETECTED,  self._detected)
        struct.pack_into(">h", buf, _OFFSET_EXPECTED,  self._expected)
        struct.pack_into(">h", buf, _OFFSET_CAMERA_ID, self._camera_id)
        struct.pack_into(">h", buf, _OFFSET_DEFECT_CT, self._defect_count)
        struct.pack_into(">h", buf, _OFFSET_OK_CT,     self._ok_count)
        h = self._batch_hash & 0xFFFFFFFF
        signed_h = h if h < 0x80000000 else h - 0x100000000
        struct.pack_into(">i", buf, _OFFSET_HASH, signed_h)
        return buf

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(0.05, remaining))


def _get_bit(byte_val: int, bit: int) -> bool:
    return bool(byte_val & (1 << bit))


def _clamp_int(value: int) -> int:
    return max(-32768, min(32767, int(value)))
