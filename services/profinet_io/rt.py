"""
services/profinet_io/rt.py — PROFINET RT Cyclic data exchange.

Builds and parses raw Ethernet RT frames (EtherType 0x8892).

Input frame  (Device → Controller, sent every cycle):
  [FrameID:2] [InputData:16] [IOPS:1] [IOCS:1]
  [CycleCounter:2] [DataStatus:1] [TransferStatus:1]
  Total payload: 22 bytes

Output frame (Controller → Device, received every cycle):
  [FrameID:2] [OutputData:1] [IOPS:1] [IOCS:1]
  [CycleCounter:2] [DataStatus:1] [TransferStatus:1]
  Total payload: 7 bytes

IOPS  = IO Provider Status  (0x80 = data valid)
IOCS  = IO Consumer Status  (0x80 = consumer running OK)
DataStatus bit layout (byte):
  bit 7: State        1 = primary
  bit 6: Redundancy   0 = no redundancy
  bit 5: DataValid    1 = data is valid
  bit 4: Reserved     0
  bit 3: Reserved     0
  bit 2: Reserved     1
  bit 1: Reserved     0
  bit 0: ProviderState 1 = run
  → 0b10100101 = 0xA5 for valid primary provider
"""

from __future__ import annotations

import struct
import threading
import time
from typing import Optional

from .constants import (
    PNIO_ETHERTYPE,
    IOPS_GOOD, IOCS_GOOD,
    DATA_STATUS_VALID, TRANSFER_STATUS_OK,
    INPUT_DATA_SIZE, OUTPUT_DATA_SIZE,
    DEFAULT_INPUT_FRAME_ID, DEFAULT_OUTPUT_FRAME_ID,
)

# Ethernet frame offsets
_ETH_DST_OFF   = 0    # 6 bytes destination MAC
_ETH_SRC_OFF   = 6    # 6 bytes source MAC
_ETH_TYPE_OFF  = 12   # 2 bytes EtherType
_ETH_PAYLOAD   = 14   # RT payload starts here

# RT frame minimum: FrameID(2) + data + IOPS(1) + IOCS(1) + APDU(4)
_RT_HEADER_SIZE   = 2   # FrameID
_RT_APDU_SIZE     = 4   # CycleCounter(2) + DataStatus(1) + TransferStatus(1)

# Broadcast MAC (used for RT multicast if needed; unicast preferred)
_RT_BROADCAST_MAC = bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])


class RTCyclic:
    """
    Builds outgoing input frames and parses incoming output frames.

    One instance is shared between the send and receive threads.
    Thread-safety: ``_lock`` protects ``_input_data`` and ``_output_data``.
    """

    def __init__(
        self,
        src_mac: str,
        dst_mac: str,
        input_frame_id:  int = DEFAULT_INPUT_FRAME_ID,
        output_frame_id: int = DEFAULT_OUTPUT_FRAME_ID,
    ) -> None:
        self._src_mac        = _parse_mac(src_mac)
        self._dst_mac        = _parse_mac(dst_mac)
        self.input_frame_id  = input_frame_id
        self.output_frame_id = output_frame_id

        self._lock        = threading.Lock()
        self._input_data  = bytearray(INPUT_DATA_SIZE)   # PC → PLC
        self._output_data = bytearray(OUTPUT_DATA_SIZE)  # PLC → PC (last received)

        self._cycle_counter: int  = 0
        self._last_output_recv: float = 0.0
        self._iocs: int = IOCS_GOOD   # reflects whether we received output OK

    # =========================================================================
    # Public write API (called from ProfinetService, UI thread via shadow buffer)
    # =========================================================================

    def set_input_data(self, data: bytes) -> None:
        """Update the 16-byte input data payload sent to the PLC each cycle."""
        with self._lock:
            self._input_data[:] = data[:INPUT_DATA_SIZE]

    def get_output_data(self) -> bytes:
        """Return the most recently received 1-byte output data from the PLC."""
        with self._lock:
            return bytes(self._output_data)

    def output_data_age_ms(self) -> float:
        """Return how many ms ago the last valid output frame was received."""
        if self._last_output_recv == 0.0:
            return float("inf")
        return (time.monotonic() - self._last_output_recv) * 1000.0

    # =========================================================================
    # Build outgoing input frame
    # =========================================================================

    def build_input_ethernet_frame(self) -> bytes:
        """
        Build a complete Ethernet frame carrying input data (Device → Controller).

        Frame structure:
          [DstMAC:6][SrcMAC:6][EtherType:2=0x8892]
          [FrameID:2][InputData:16][IOPS:1][IOCS:1]
          [CycleCounter:2][DataStatus:1][TransferStatus:1]
        """
        with self._lock:
            payload = bytes(self._input_data)
            iocs    = self._iocs
            cc      = self._cycle_counter
            # Advance cycle counter (wraps at 0x10000)
            self._cycle_counter = (cc + 1) & 0xFFFF

        rt_payload = (
            struct.pack("!H", self.input_frame_id)
            + payload                          # 16 bytes input data
            + bytes([IOPS_GOOD])               # our provider status
            + bytes([iocs])                    # consumer status for output CR
            + struct.pack("!HBB", cc, DATA_STATUS_VALID, TRANSFER_STATUS_OK)
        )

        eth_header = self._dst_mac + self._src_mac + struct.pack("!H", PNIO_ETHERTYPE)
        return eth_header + rt_payload

    # =========================================================================
    # Parse incoming output frame
    # =========================================================================

    def parse_ethernet_frame(self, frame: bytes) -> Optional[bytes]:
        """
        Check whether ``frame`` is an RT output frame addressed to this device.

        If valid, extract the output data bytes, update internal state, and
        return the output data.  Returns None if the frame should be ignored.
        """
        if len(frame) < _ETH_PAYLOAD + _RT_HEADER_SIZE + OUTPUT_DATA_SIZE + 2 + _RT_APDU_SIZE:
            return None

        # EtherType check
        eth_type = struct.unpack_from("!H", frame, _ETH_TYPE_OFF)[0]
        if eth_type != PNIO_ETHERTYPE:
            return None

        # FrameID check
        frame_id = struct.unpack_from("!H", frame, _ETH_PAYLOAD)[0]
        if frame_id != self.output_frame_id:
            return None

        # Destination MAC check (unicast to us OR broadcast)
        dst_mac = frame[:6]
        if dst_mac != self._src_mac and dst_mac != _RT_BROADCAST_MAC:
            return None

        # Extract output data
        data_start  = _ETH_PAYLOAD + _RT_HEADER_SIZE
        output_data = frame[data_start: data_start + OUTPUT_DATA_SIZE]

        # Provider IOPS from controller (byte after output data)
        controller_iops = frame[data_start + OUTPUT_DATA_SIZE]
        our_iocs = IOCS_GOOD if (controller_iops & 0x80) else 0x00

        with self._lock:
            self._output_data[:] = output_data
            self._iocs = our_iocs
            self._last_output_recv = time.monotonic()

        return bytes(output_data)

    # =========================================================================
    # Frame ID update (from CM negotiation)
    # =========================================================================

    def update_frame_ids(self, input_frame_id: int, output_frame_id: int) -> None:
        """Update frame IDs after CM Connect negotiation."""
        self.input_frame_id  = input_frame_id
        self.output_frame_id = output_frame_id

    def update_dst_mac(self, dst_mac: str) -> None:
        """Update the destination MAC (controller's MAC from CM AR block)."""
        with self._lock:
            self._dst_mac = _parse_mac(dst_mac)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _parse_mac(mac: str) -> bytes:
    """Convert 'AA:BB:CC:DD:EE:FF' to 6-byte bytes."""
    return bytes(int(x, 16) for x in mac.split(":"))
