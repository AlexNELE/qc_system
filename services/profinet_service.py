"""
services/profinet_service.py — PROFINET IO Device service (Mode B).

Orchestrates the three protocol layers:
  - DCP   : raw Ethernet 0x8892, Scapy AsyncSniffer, discovery + config
  - CM    : UDP port 34964, DCE/RPC, Application Relationship management
  - RT    : raw Ethernet 0x8892, Scapy sendp, cyclic input/output frames

State machine:
  OFFLINE → STANDBY → DATA_EXCHANGE → OFFLINE (on AR release / error)

Public API mirrors PLCService so MainWindow can use either interchangeably:
  Signals : trigger_received(int), plc_connected(), plc_disconnected(), plc_error(str)
  Methods : write_result(), write_batch_state(), write_system_ready(), write_ack_clear()
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scapy import — optional; service degrades gracefully if absent
# ---------------------------------------------------------------------------
try:
    from scapy.all import AsyncSniffer, sendp, Ether, conf as scapy_conf  # type: ignore
    _SCAPY_AVAILABLE = True
except ImportError:
    _SCAPY_AVAILABLE = False
    logger.warning("scapy not installed — ProfinetService (Mode B) will not function")

from .profinet_io.constants import (
    PNIO_ETHERTYPE,
    PNIO_CM_UDP_PORT,
    FRAME_ID_DCP_IDENTIFY_REQ, FRAME_ID_DCP_IDENTIFY_RES,
    FRAME_ID_DCP_GETSET, FRAME_ID_DCP_HELLO,
    INPUT_DATA_SIZE, OUTPUT_DATA_SIZE,
    DEFAULT_INPUT_FRAME_ID, DEFAULT_OUTPUT_FRAME_ID,
)
from .profinet_io.dcp import DCPHandler
from .profinet_io.cm  import CMHandler
from .profinet_io.rt  import RTCyclic

# ---------------------------------------------------------------------------
# DB100 bit layout (identical to PLCService for drop-in compatibility)
# ---------------------------------------------------------------------------
# Input byte 0 — PC → PLC (flags)
_BIT_SYSTEM_READY   = 0
_BIT_BATCH_ACTIVE   = 1
_BIT_CAPTURE_OK     = 2
_BIT_CAPTURE_BUSY   = 3
_BIT_ACK_TRIGGER    = 6

# Output byte 0 — PLC → PC (flags)
_BIT_TRIGGER        = 0
_BIT_INHIBIT        = 7

# Word offsets inside INPUT_DATA_SIZE=16 byte block (big-endian)
_OFF_DETECTED       = 2   # DBW2  detected_count   (uint16)
_OFF_EXPECTED       = 4   # DBW4  expected_count    (uint16)
_OFF_CAMERA_ID      = 6   # DBW6  camera_id         (uint16)
_OFF_DEFECT         = 8   # DBW8  defect_count      (uint16)
_OFF_OK             = 10  # DBW10 ok_count           (uint16)
_OFF_BATCH_HASH     = 12  # DBD12 batch_id_hash     (uint32)


def _get_bit(byte: int, bit: int) -> bool:
    return bool(byte & (1 << bit))


def _set_bit(byte: int, bit: int, value: bool) -> int:
    if value:
        return byte | (1 << bit)
    return byte & ~(1 << bit)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class _State(Enum):
    OFFLINE       = auto()   # no AR
    STANDBY       = auto()   # DCP/CM ready, waiting for AR
    DATA_EXCHANGE = auto()   # AR established, RT running


# ---------------------------------------------------------------------------
# ProfinetService
# ---------------------------------------------------------------------------

class ProfinetService(QThread):
    """
    PROFINET IO Device service (Mode B).

    Drop-in replacement for PLCService:
      - same signals: trigger_received, plc_connected, plc_disconnected, plc_error
      - same write methods: write_result, write_batch_state, write_system_ready, write_ack_clear
    """

    trigger_received  = Signal(int)   # camera_id (-1 = all)
    plc_connected     = Signal()
    plc_disconnected  = Signal()
    plc_error         = Signal(str)

    def __init__(
        self,
        interface:    str,
        station_name: str,
        mac_address:  str,
        ip_address:   str,
        subnet_mask:  str,
        gateway:      str,
        cycle_time_ms: int = 4,
        watchdog_ms:   int = 200,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._interface    = interface
        self._cycle_time_s = cycle_time_ms / 1000.0
        self._watchdog_ms  = watchdog_ms

        self._state = _State.OFFLINE
        self._stop_event = threading.Event()

        # Shadow input buffer (16 bytes, PC → PLC)
        self._lock       = threading.Lock()
        self._input_buf  = bytearray(INPUT_DATA_SIZE)

        # Batch ID hash cache (avoids redundant CRC32)
        self._batch_id_cache: str = ""
        self._batch_hash: int     = 0

        # Last trigger state for edge detection
        self._last_trigger = False

        # Protocol layer instances
        self._dcp = DCPHandler(
            station_name=station_name,
            mac_address=mac_address,
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            gateway=gateway,
        )
        self._cm  = CMHandler()
        self._rt  = RTCyclic(
            src_mac=mac_address,
            dst_mac=mac_address,          # overwritten when AR established
            input_frame_id=DEFAULT_INPUT_FRAME_ID,
            output_frame_id=DEFAULT_OUTPUT_FRAME_ID,
        )

        # Wire up CM callbacks
        self._cm.on_ar_established = self._on_ar_established
        self._cm.on_ar_released    = self._on_ar_released

        # Wire up DCP callbacks
        self._dcp.on_name_changed = self._on_dcp_name_changed
        self._dcp.on_ip_changed   = self._on_dcp_ip_changed

        # UDP socket for CM (DCE/RPC)
        self._udp_sock: Optional[socket.socket] = None

        # Scapy sniffer handle
        self._sniffer = None

        # RT send thread handle
        self._rt_thread: Optional[threading.Thread] = None

    # =========================================================================
    # QThread entry point
    # =========================================================================

    def run(self) -> None:
        if not _SCAPY_AVAILABLE:
            self.plc_error.emit("scapy not installed — install with: pip install scapy")
            return

        logger.info("ProfinetService starting on interface '%s'", self._interface)

        try:
            self._start_udp_server()
            self._start_sniffer()
            self._state = _State.STANDBY
            logger.info("ProfinetService STANDBY — waiting for controller AR")

            # Main loop: watchdog + stop check
            while not self._stop_event.is_set():
                if self._state == _State.DATA_EXCHANGE:
                    age = self._rt.output_data_age_ms()
                    if age > self._watchdog_ms:
                        logger.warning(
                            "PROFINET watchdog: no output frame for %.0f ms — going OFFLINE", age
                        )
                        self._teardown_ar()

                time.sleep(0.05)

        except Exception as exc:
            logger.exception("ProfinetService fatal error: %s", exc)
            self.plc_error.emit(str(exc))
        finally:
            self._cleanup()
            logger.info("ProfinetService stopped")

    def stop(self) -> None:
        """Request the service to stop (called from UI thread)."""
        self._stop_event.set()
        self.quit()
        self.wait(3000)

    # =========================================================================
    # Internal — startup / shutdown helpers
    # =========================================================================

    def _start_udp_server(self) -> None:
        """Bind UDP socket for DCE/RPC CM messages."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", PNIO_CM_UDP_PORT))
        sock.settimeout(0.5)
        self._udp_sock = sock

        t = threading.Thread(target=self._udp_recv_loop, daemon=True, name="pnio-cm-udp")
        t.start()
        logger.debug("PNIO CM UDP listener started on port %d", PNIO_CM_UDP_PORT)

    def _start_sniffer(self) -> None:
        """Start Scapy AsyncSniffer for EtherType 0x8892."""
        bpf = f"ether proto 0x8892"
        self._sniffer = AsyncSniffer(
            iface=self._interface,
            filter=bpf,
            prn=self._on_raw_ethernet,
            store=False,
        )
        self._sniffer.start()
        logger.debug("Scapy sniffer started on iface '%s'", self._interface)

    def _cleanup(self) -> None:
        """Stop all background threads and close sockets."""
        # Stop sniffer
        try:
            if self._sniffer is not None:
                self._sniffer.stop()
        except Exception:
            pass

        # Stop RT thread
        self._stop_rt_thread()

        # Close UDP socket
        try:
            if self._udp_sock is not None:
                self._udp_sock.close()
        except Exception:
            pass

    # =========================================================================
    # Raw Ethernet receive (Scapy callback — runs in sniffer thread)
    # =========================================================================

    def _on_raw_ethernet(self, pkt) -> None:
        """Called by Scapy for every captured 0x8892 frame."""
        try:
            raw: bytes = bytes(pkt)
            if len(raw) < 16:
                return

            src_mac  = ":".join(f"{b:02X}" for b in raw[6:12])
            frame_id = struct.unpack_from("!H", raw, 14)[0]

            # DCP frames
            if FRAME_ID_DCP_HELLO <= frame_id <= FRAME_ID_DCP_IDENTIFY_REQ:
                self._handle_dcp_frame(raw[14:], src_mac)
                return

            if frame_id == FRAME_ID_DCP_GETSET:
                self._handle_dcp_frame(raw[14:], src_mac)
                return

            # RT output frame (controller → device)
            if self._state == _State.DATA_EXCHANGE:
                output = self._rt.parse_ethernet_frame(raw)
                if output is not None:
                    self._process_output_byte(output[0])

        except Exception as exc:
            logger.debug("Error in _on_raw_ethernet: %s", exc)

    def _handle_dcp_frame(self, payload: bytes, src_mac: str) -> None:
        """Feed payload (from FrameID) to DCPHandler; send response if needed."""
        response = self._dcp.handle_frame(payload, src_mac)
        if response is None:
            return

        try:
            dst_bytes = bytes(int(x, 16) for x in src_mac.split(":"))
            src_bytes = bytes(int(x, 16) for x in self._dcp.mac_address.split(":"))
            eth_type  = struct.pack("!H", PNIO_ETHERTYPE)
            frame     = dst_bytes + src_bytes + eth_type + response
            sendp(Ether(frame), iface=self._interface, verbose=False)
        except Exception as exc:
            logger.warning("Failed to send DCP response: %s", exc)

    # =========================================================================
    # UDP receive loop — DCE/RPC CM (runs in daemon thread)
    # =========================================================================

    def _udp_recv_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, addr = self._udp_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                response = self._cm.handle_udp(data, addr[0])
                if response is not None:
                    self._udp_sock.sendto(response, addr)
            except Exception as exc:
                logger.warning("CM UDP error from %s: %s", addr, exc)

    # =========================================================================
    # CM callbacks (called from UDP thread)
    # =========================================================================

    def _on_ar_established(self) -> None:
        """AR is up — update RT frame IDs and start cyclic sending."""
        ar = self._cm.active_ar
        if ar is None:
            return

        logger.info(
            "AR established — controller %s, input_fid=0x%04X, output_fid=0x%04X",
            ar.controller_mac,
            ar.input_iocr.frame_id if ar.input_iocr else DEFAULT_INPUT_FRAME_ID,
            ar.output_iocr.frame_id if ar.output_iocr else DEFAULT_OUTPUT_FRAME_ID,
        )

        in_fid  = ar.input_iocr.frame_id  if ar.input_iocr  else DEFAULT_INPUT_FRAME_ID
        out_fid = ar.output_iocr.frame_id if ar.output_iocr else DEFAULT_OUTPUT_FRAME_ID

        self._rt.update_frame_ids(in_fid, out_fid)
        self._rt.update_dst_mac(ar.controller_mac)

        self._state = _State.DATA_EXCHANGE
        self._start_rt_thread()
        self.plc_connected.emit()

    def _on_ar_released(self) -> None:
        """AR released by controller."""
        logger.info("AR released by controller — going STANDBY")
        self._teardown_ar()

    def _teardown_ar(self) -> None:
        """Tear down active AR and return to STANDBY."""
        self._stop_rt_thread()
        self._state = _State.STANDBY
        # Clear flags so PLC sees system as offline
        with self._lock:
            self._input_buf[:] = bytearray(INPUT_DATA_SIZE)
        self._rt.set_input_data(bytes(INPUT_DATA_SIZE))
        self.plc_disconnected.emit()

    # =========================================================================
    # DCP callbacks (called from sniffer thread)
    # =========================================================================

    def _on_dcp_name_changed(self, new_name: str) -> None:
        logger.info("Station name changed to '%s' via DCP", new_name)

    def _on_dcp_ip_changed(self, ip: str, subnet: str, gateway: str) -> None:
        logger.info("IP changed to %s/%s gw=%s via DCP", ip, subnet, gateway)

    # =========================================================================
    # RT cyclic send thread
    # =========================================================================

    def _start_rt_thread(self) -> None:
        self._stop_rt_event = threading.Event()
        t = threading.Thread(
            target=self._rt_send_loop,
            daemon=True,
            name="pnio-rt-send",
        )
        self._rt_thread = t
        t.start()

    def _stop_rt_thread(self) -> None:
        if hasattr(self, "_stop_rt_event"):
            self._stop_rt_event.set()
        if self._rt_thread is not None:
            self._rt_thread.join(timeout=1.0)
            self._rt_thread = None

    def _rt_send_loop(self) -> None:
        """Send input frames to controller at cycle_time_s intervals."""
        logger.debug("RT send loop started (%.1f ms cycle)", self._cycle_time_s * 1000)
        while not self._stop_rt_event.is_set() and not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                frame = self._rt.build_input_ethernet_frame()
                sendp(Ether(frame), iface=self._interface, verbose=False)
            except Exception as exc:
                logger.debug("RT send error: %s", exc)

            elapsed = time.monotonic() - t0
            sleep   = self._cycle_time_s - elapsed
            if sleep > 0:
                time.sleep(sleep)

        logger.debug("RT send loop stopped")

    # =========================================================================
    # Output byte processing (edge detection for trigger + inhibit)
    # =========================================================================

    def _process_output_byte(self, byte: int) -> None:
        """Detect rising trigger edge; respect inhibit bit."""
        trigger_now = _get_bit(byte, _BIT_TRIGGER)
        inhibit_now = _get_bit(byte, _BIT_INHIBIT)

        if trigger_now and not self._last_trigger:
            if inhibit_now:
                logger.warning("PROFINET trigger received but inhibit bit set — suppressed")
            else:
                with self._lock:
                    flag = self._input_buf[0]
                    self._input_buf[0] = _set_bit(flag, _BIT_ACK_TRIGGER, True)
                self._flush_input()
                self.trigger_received.emit(-1)

        self._last_trigger = trigger_now

    # =========================================================================
    # Public write API (matches PLCService)
    # =========================================================================

    def write_result(
        self,
        camera_id:      int,
        detected_count: int,
        defect_count:   int,
        ok_count:       int,
        capture_ok:     bool = True,
    ) -> None:
        """Write per-camera inspection result into the input shadow buffer."""
        with self._lock:
            buf = self._input_buf
            buf[0] = _set_bit(buf[0], _BIT_CAPTURE_OK,   capture_ok)
            buf[0] = _set_bit(buf[0], _BIT_CAPTURE_BUSY, False)
            struct.pack_into("!H", buf, _OFF_CAMERA_ID,  _clamp(camera_id,      0, 0xFFFF))
            struct.pack_into("!H", buf, _OFF_DETECTED,   _clamp(detected_count, 0, 0xFFFF))
            struct.pack_into("!H", buf, _OFF_DEFECT,     _clamp(defect_count,   0, 0xFFFF))
            struct.pack_into("!H", buf, _OFF_OK,         _clamp(ok_count,       0, 0xFFFF))
        self._flush_input()

    def write_batch_state(
        self,
        active:         bool,
        batch_id:       str  = "",
        expected_count: int  = 0,
        detected_count: int  = 0,
        defect_count:   int  = 0,
        ok_count:       int  = 0,
    ) -> None:
        """Write batch state and counters into the input shadow buffer."""
        # Compute batch hash only when batch_id changes
        if batch_id != self._batch_id_cache:
            self._batch_id_cache = batch_id
            self._batch_hash = zlib.crc32(batch_id.encode()) if batch_id else 0

        with self._lock:
            buf = self._input_buf
            buf[0] = _set_bit(buf[0], _BIT_BATCH_ACTIVE, active)
            struct.pack_into("!H", buf, _OFF_EXPECTED, _clamp(expected_count, 0, 0xFFFF))
            struct.pack_into("!H", buf, _OFF_DETECTED, _clamp(detected_count, 0, 0xFFFF))
            struct.pack_into("!H", buf, _OFF_DEFECT,   _clamp(defect_count,   0, 0xFFFF))
            struct.pack_into("!H", buf, _OFF_OK,       _clamp(ok_count,       0, 0xFFFF))
            struct.pack_into("!I", buf, _OFF_BATCH_HASH, self._batch_hash & 0xFFFFFFFF)
        self._flush_input()

    def write_system_ready(self, ready: bool) -> None:
        """Set/clear the system-ready flag in the input shadow buffer."""
        with self._lock:
            self._input_buf[0] = _set_bit(self._input_buf[0], _BIT_SYSTEM_READY, ready)
        self._flush_input()

    def write_ack_clear(self) -> None:
        """Clear the ACK-trigger bit after capture completes (called from UI thread)."""
        with self._lock:
            self._input_buf[0] = _set_bit(self._input_buf[0], _BIT_ACK_TRIGGER, False)
        self._flush_input()

    # =========================================================================
    # Internal — flush shadow buffer → RT layer
    # =========================================================================

    def _flush_input(self) -> None:
        """Copy shadow input buffer into RTCyclic (thread-safe snapshot)."""
        with self._lock:
            data = bytes(self._input_buf)
        self._rt.set_input_data(data)
