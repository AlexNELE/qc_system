"""
services/plc_service.py — Siemens S7-1500 PLC communication service.

Uses the python-snap7 library (ISO TCP, port 102) to exchange data with a
Siemens S7-1500 (or compatible S7-1200 / S7-400 / S7-300) PLC via a shared
Data Block.

Data Block layout (default DB100, 16 bytes total):

  ┌──────────┬───────┬──────────────────┬───────────┬────────────────────────────────────┐
  │ Offset   │ Type  │ Name             │ Direction │ Description                        │
  ├──────────┼───────┼──────────────────┼───────────┼────────────────────────────────────┤
  │ DBX 0.0  │ BOOL  │ trigger          │ PLC → PC  │ Rising edge → capture all cameras  │
  │ DBX 0.1  │ BOOL  │ batch_active     │ PC → PLC  │ A batch is currently running       │
  │ DBX 0.2  │ BOOL  │ result_ok        │ PC → PLC  │ Last capture result is OK          │
  │ DBX 0.3  │ BOOL  │ result_defect    │ PC → PLC  │ Last capture result is DEFECT      │
  │ DBX 0.4  │ BOOL  │ heartbeat        │ PC → PLC  │ Toggles every second (PC alive)    │
  │ DBX 0.5  │ BOOL  │ system_ready     │ PC → PLC  │ All cameras initialised            │
  │ DBX 0.6  │ BOOL  │ ack_trigger      │ PC → PLC  │ Set while processing PLC trigger   │
  │ DBX 0.7  │ BOOL  │ (spare)          │ –         │ Reserved for future use            │
  ├──────────┼───────┼──────────────────┼───────────┼────────────────────────────────────┤
  │ DBB 1    │ BYTE  │ (padding)        │ –         │ Word-alignment pad                 │
  ├──────────┼───────┼──────────────────┼───────────┼────────────────────────────────────┤
  │ DBW 2    │ INT   │ detected_count   │ PC → PLC  │ Detected items in last capture     │
  │ DBW 4    │ INT   │ expected_count   │ PC → PLC  │ Configured expected item count     │
  │ DBW 6    │ INT   │ camera_id        │ PC → PLC  │ Camera that produced last result   │
  │ DBW 8    │ INT   │ defect_count     │ PC → PLC  │ Running MISSING tally (batch)      │
  │ DBW 10   │ INT   │ ok_count         │ PC → PLC  │ Running OK tally (batch)           │
  │ DBD 12   │ DINT  │ batch_id_hash    │ PC → PLC  │ CRC-32 of current batch ID string  │
  └──────────┴───────┴──────────────────┴───────────┴────────────────────────────────────┘

Bit 0.0 (trigger) is written by the PLC and read-only for the PC.  All other
bits in byte 0 are written by the PC.  On every write cycle the PC preserves
bit 0.0 from the last read so PLC output is never inadvertently cleared.

Snap7 dependency
----------------
python-snap7 wraps the native snap7 DLL (Windows) / .so (Linux).  Since
python-snap7 >= 1.3 the DLL is bundled with the wheel, so no separate DLL
install is required on Windows.

Install::

    pip install python-snap7

If the package is not installed PLCService logs a warning and its run() loop
exits immediately — the rest of the application continues unaffected.

PLC-side TIA Portal configuration
----------------------------------
1.  Create a Global DB (e.g. DB100) with the layout above.
2.  Set the DB to **non-optimised block access** (uncheck "Optimized block
    access" in block properties) so that byte offsets are fixed.
3.  Ensure the S7-1500 CPU has **PUT/GET communication** enabled:
    Properties → Protection & Security → Connection mechanisms →
    [x] Permit access with PUT/GET communication from remote partner.
4.  The PC IP address must be reachable from the PLC (same subnet or routed).

Multi-camera trigger scope
--------------------------
The single trigger bit (DBX 0.0) maps to "capture all cameras" simultaneously.
This is the correct pattern when all cameras inspect the same tray position
together.  For independent per-camera triggers, byte 1 (currently zero-padded)
provides 8 spare bits (one per camera lane) that can be mapped to individual
capture actions without increasing the DB size.

Heartbeat & PLC watchdog
------------------------
The PC heartbeat bit (DBX 0.4) toggles every 1000 ms.  Configure a PLC
watchdog function block with a 3000 ms timeout to detect a PC crash or
network loss and respond with a safe conveyor stop.

Timing note
-----------
batch_active (DBX 0.1) and system_ready (DBX 0.5) are written to the
shadow buffer immediately on batch start, but are not visible to the PLC
until the next poll cycle (~50 ms).  Allow at least 100 ms after observing
a rising edge on batch_active before asserting the trigger.
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
# Optional snap7 import — graceful degradation when not installed
# ---------------------------------------------------------------------------
try:
    import snap7
    _SNAP7_AVAILABLE = True
except ImportError:
    _SNAP7_AVAILABLE = False
    logger.warning(
        "python-snap7 is not installed — PLCService will be disabled.  "
        "Install with:  pip install python-snap7"
    )

# ---------------------------------------------------------------------------
# DB byte / bit offset constants
# ---------------------------------------------------------------------------
_DB_SIZE = 16           # total bytes used in the Data Block

_BYTE_FLAGS = 0         # byte 0: all boolean flags

# Bit positions within byte 0
_BIT_TRIGGER       = 0  # PLC → PC  (read-only for PC)
_BIT_BATCH_ACTIVE  = 1  # PC → PLC
_BIT_RESULT_OK     = 2  # PC → PLC
_BIT_RESULT_DEFECT = 3  # PC → PLC
_BIT_HEARTBEAT     = 4  # PC → PLC  (toggled every second)
_BIT_SYSTEM_READY  = 5  # PC → PLC
_BIT_ACK_TRIGGER   = 6  # PC → PLC  (high while trigger is being processed)
_BIT_INHIBIT       = 7  # PLC → PC  (spare / E-Stop inhibit; active high = inhibit capture)

# Word / DWord offsets (big-endian, matching S7 memory layout)
_OFFSET_DETECTED  = 2   # DBW 2  — INT  detected_count
_OFFSET_EXPECTED  = 4   # DBW 4  — INT  expected_count
_OFFSET_CAMERA_ID = 6   # DBW 6  — INT  camera_id  (-1 = all cameras)
_OFFSET_DEFECT_CT = 8   # DBW 8  — INT  defect_count (batch running tally)
_OFFSET_OK_CT     = 10  # DBW 10 — INT  ok_count    (batch running tally)
_OFFSET_HASH      = 12  # DBD 12 — DINT batch_id_hash (CRC-32)


class PLCService(QThread):
    """
    Background QThread that polls a Siemens S7-1500 Data Block.

    The thread connects to the PLC over Ethernet (ISO TCP, port 102),
    then enters a tight poll loop:

    1.  Read the full DB (16 bytes).
    2.  Detect a rising edge on bit 0.0 (trigger written by the PLC).
        If detected: emit ``trigger_received(-1)`` so MainWindow can call
        _capture_all() on the UI thread.
    3.  Toggle the heartbeat bit once per second.
    4.  Write the PC-owned output bytes back to the PLC.

    When the connection drops the thread reconnects with exponential back-off
    (configurable via settings.json).

    All public ``write_*`` methods are thread-safe and may be called from any
    thread (typically the UI thread).

    Signals
    -------
    trigger_received(camera_id: int)
        Rising edge on PLC trigger bit.  ``camera_id`` is always -1 in the
        current implementation (meaning "capture all cameras"); extend as
        needed for per-camera triggers.
    plc_connected()
        Emitted on initial connect and on each successful reconnect.
    plc_disconnected()
        Emitted when the connection is lost.
    plc_error(message: str)
        Emitted for non-fatal errors (connection failures, read/write errors).
        Already written to the log; the UI may show these in the status bar.
    """

    trigger_received = Signal(int)   # -1 = all cameras
    plc_connected    = Signal()
    plc_disconnected = Signal()
    plc_error        = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._stop_event = threading.Event()
        self._lock       = threading.Lock()

        # ── Shadow output state (what the PC writes to the PLC) ──────────────
        # Byte 0 output bits — only bits 1-7 are PC-owned; bit 0 is PLC-owned.
        self._flag_byte:    int = 0x00

        # INT fields (clamped to S7 INT range: -32768 … 32767)
        self._detected:     int = 0
        self._expected:     int = settings.EXPECTED_COUNT
        self._camera_id:    int = -1
        self._defect_count: int = 0
        self._ok_count:     int = 0

        # DINT field (batch CRC-32, stored as Python int, written as signed 32-bit)
        self._batch_hash:   int = 0
        self._batch_id_cache: str = ""   # last seen batch_id for hash caching

        # ── Internal state ────────────────────────────────────────────────────
        self._last_trigger:       bool  = False   # previous poll's trigger state
        self._heartbeat_state:    bool  = False
        self._last_heartbeat_flip: float = 0.0

    # =========================================================================
    # Public write API  (called from MainWindow on the UI thread)
    # =========================================================================

    def write_result(
        self,
        camera_id: int,
        status: str,
        detected: int,
        expected: int,
    ) -> None:
        """
        Push the latest capture result to the PLC shadow buffer.

        Parameters
        ----------
        camera_id:
            Logical camera index (0-based).
        status:
            ``"OK"`` or ``"MISSING"``.
        detected:
            Number of objects detected in this capture.
        expected:
            Configured expected object count.
        """
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
        """
        Notify the PLC of a batch start / end event and running tallies.

        Parameters
        ----------
        active:
            ``True`` when a batch starts; ``False`` when it ends.
        ok_count:
            Cumulative OK captures since batch start.
        defect_count:
            Cumulative MISSING captures since batch start.
        batch_id:
            The batch ID string; its CRC-32 is written to DBD12.
        """
        with self._lock:
            self._set_flag(_BIT_BATCH_ACTIVE, active)
            self._ok_count     = _clamp_int(ok_count)
            self._defect_count = _clamp_int(defect_count)
            if batch_id != self._batch_id_cache:
                self._batch_id_cache = batch_id
                self._batch_hash = zlib.crc32(batch_id.encode()) if batch_id else 0
            if not active:
                # Clear per-result bits when the batch ends
                self._set_flag(_BIT_RESULT_OK,     False)
                self._set_flag(_BIT_RESULT_DEFECT, False)
                self._detected  = 0
                self._camera_id = -1

    def write_system_ready(self, ready: bool) -> None:
        """
        Set or clear the ``system_ready`` flag (bit 0.5).

        Call with ``True`` after all cameras have successfully initialised,
        and ``False`` when cameras are stopped.
        """
        with self._lock:
            self._set_flag(_BIT_SYSTEM_READY, ready)

    def write_ack_clear(self) -> None:
        """
        Clear the ack_trigger bit after the UI thread has finished processing
        the PLC trigger.  Called explicitly from MainWindow._on_plc_trigger
        after _capture_all() returns so the ACK pulse is stable for the full
        duration of the capture operation.
        """
        with self._lock:
            self._set_flag(_BIT_ACK_TRIGGER, False)

    # =========================================================================
    # QThread entry point
    # =========================================================================

    def run(self) -> None:
        """Main poll loop — executes in the background thread."""
        if not _SNAP7_AVAILABLE:
            logger.warning(
                "PLCService: python-snap7 not available — thread exiting.  "
                "Install with: pip install python-snap7"
            )
            return

        ip            = settings.PLC_IP
        rack          = settings.PLC_RACK
        slot          = settings.PLC_SLOT
        db_number     = settings.PLC_DB_NUMBER
        poll_interval = settings.PLC_POLL_INTERVAL_MS / 1000.0
        reconnect     = settings.PLC_RECONNECT_DELAY
        reconnect_max = settings.PLC_RECONNECT_MAX

        logger.info(
            "PLCService starting | ip=%s rack=%d slot=%d db=DB%d poll=%dms",
            ip, rack, slot, db_number, settings.PLC_POLL_INTERVAL_MS,
        )

        client    = snap7.client.Client()
        connected = False

        while not self._stop_event.is_set():

            # ------------------------------------------------------------------
            # Connection phase — retry with exponential back-off
            # ------------------------------------------------------------------
            if not connected:
                try:
                    client.connect(ip, rack, slot)
                    connected = True
                    reconnect = settings.PLC_RECONNECT_DELAY  # reset back-off
                    logger.info("PLC connected | ip=%s db=DB%d", ip, db_number)
                    self.plc_connected.emit()
                except Exception as exc:
                    msg = f"PLC connect failed ({ip}): {exc}"
                    logger.warning("%s — retrying in %.1f s", msg, reconnect)
                    self.plc_error.emit(msg)
                    self._interruptible_sleep(reconnect)
                    reconnect = min(reconnect * 2.0, reconnect_max)
                    continue

            # ------------------------------------------------------------------
            # Poll phase — read → process → write
            # ------------------------------------------------------------------
            try:
                # 1. Read the full DB area from the PLC
                raw: bytearray = client.db_read(db_number, 0, _DB_SIZE)

                # 2. Detect rising edge on trigger bit (PLC → PC, bit 0.0)
                trigger_now = _get_bit(raw[0], _BIT_TRIGGER)
                inhibit_now = _get_bit(raw[0], _BIT_INHIBIT)
                if trigger_now and not self._last_trigger:
                    if inhibit_now:
                        logger.warning(
                            "PLC trigger received but inhibit bit is set — capture suppressed"
                        )
                    else:
                        logger.info(
                            "PLC trigger rising edge | db=DB%d — emitting trigger_received",
                            db_number,
                        )
                        with self._lock:
                            self._set_flag(_BIT_ACK_TRIGGER, True)
                        self.trigger_received.emit(-1)   # -1 = capture all cameras
                # ACK is cleared explicitly by write_ack_clear() after the UI
                # thread completes capture — do NOT auto-clear on falling edge.
                self._last_trigger = trigger_now

                # 3. Toggle heartbeat once per second
                now = time.monotonic()
                if now - self._last_heartbeat_flip >= 1.0:
                    self._heartbeat_state     = not self._heartbeat_state
                    self._last_heartbeat_flip = now
                    with self._lock:
                        self._set_flag(_BIT_HEARTBEAT, self._heartbeat_state)

                # 4. Serialise shadow state and write back to PLC
                with self._lock:
                    buf = self._build_write_buffer()

                # Preserve the PLC-written trigger bit (bit 0) so we never
                # overwrite a trigger the PLC is currently asserting.
                buf[0] = (buf[0] & ~(1 << _BIT_TRIGGER)) | (raw[0] & (1 << _BIT_TRIGGER))

                client.db_write(db_number, 0, buf)

            except Exception as exc:
                msg = f"PLC communication error: {exc}"
                logger.warning("%s — reconnecting", msg)
                self.plc_error.emit(msg)
                try:
                    client.disconnect()
                except Exception as exc:
                    logger.debug("PLC disconnect error (ignored): %s", exc)
                connected = False
                self.plc_disconnected.emit()
                self._interruptible_sleep(reconnect)
                reconnect = min(reconnect * 2.0, reconnect_max)
                continue

            self._interruptible_sleep(poll_interval)

        # ----------------------------------------------------------------------
        # Shutdown — clear PC outputs so PLC sees a clean state on disconnect
        # ----------------------------------------------------------------------
        if connected:
            try:
                with self._lock:
                    self._flag_byte = 0x00  # clear all PC-owned bits
                    buf = self._build_write_buffer()
                client.db_write(db_number, 0, buf)
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception as exc:
                logger.debug("PLC disconnect error (ignored): %s", exc)

        logger.info("PLCService stopped")

    def stop(self) -> None:
        """Request the thread to stop and wait up to 5 s."""
        self._stop_event.set()
        if not self.wait(5000):
            logger.warning("PLCService did not stop cleanly — terminating")
            self.terminate()

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _set_flag(self, bit: int, value: bool) -> None:
        """Set or clear a bit in the shadow flag byte.  Caller must hold _lock."""
        if value:
            self._flag_byte |=  (1 << bit)
        else:
            self._flag_byte &= ~(1 << bit)

    def _build_write_buffer(self) -> bytearray:
        """
        Serialise the current shadow state into a 16-byte write buffer.

        Caller must hold ``_lock``.  The buffer maps directly onto the
        S7 Data Block layout documented at the top of this module.
        """
        buf = bytearray(_DB_SIZE)

        # Byte 0 — boolean flags (bit 0 placeholder; caller patches it)
        buf[0] = self._flag_byte & 0xFF

        # Byte 1 — padding (leave as 0)

        # INT fields — big-endian signed 16-bit (S7 INT)
        struct.pack_into(">h", buf, _OFFSET_DETECTED,  self._detected)
        struct.pack_into(">h", buf, _OFFSET_EXPECTED,  self._expected)
        struct.pack_into(">h", buf, _OFFSET_CAMERA_ID, self._camera_id)
        struct.pack_into(">h", buf, _OFFSET_DEFECT_CT, self._defect_count)
        struct.pack_into(">h", buf, _OFFSET_OK_CT,     self._ok_count)

        # DINT field — big-endian signed 32-bit (S7 DINT)
        # CRC-32 is unsigned 32-bit; convert to signed for struct.pack
        h = self._batch_hash & 0xFFFFFFFF
        signed_h = h if h < 0x80000000 else h - 0x100000000
        struct.pack_into(">i", buf, _OFFSET_HASH, signed_h)

        return buf

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in 50 ms slices so stop() is responsive within ~50 ms."""
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(0.05, remaining))


# ---------------------------------------------------------------------------
# Module-level helper (pure function, no state)
# ---------------------------------------------------------------------------

def _get_bit(byte_val: int, bit: int) -> bool:
    """Return True if ``bit`` is set in ``byte_val``."""
    return bool(byte_val & (1 << bit))


def _clamp_int(value: int) -> int:
    """Clamp ``value`` to the S7 INT range [-32768, 32767]."""
    return max(-32768, min(32767, int(value)))
