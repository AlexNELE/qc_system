"""
services/profinet_io/cm.py — PROFINET IO Connection Management (DCE/RPC over UDP).

The IO Controller (S7-1500) initiates the Application Relationship (AR) by
sending a Connect request via DCE/RPC.  This module parses that request,
stores the negotiated IO CR frame IDs, and builds valid responses so TIA
Portal can successfully download and start the device.

DCE/RPC header structure (fixed 80 bytes, then NDR payload):
  [Version:1][PacketType:1][Flags:1][Flags2:1][DataRep:4][SerialHigh:1]
  [ObjectUUID:16][InterfaceUUID:16][ActivityUUID:16][ServerBootTime:4]
  [InterfaceVersion:4][SequenceNum:4][OpNum:2][InterfaceHint:2]
  [ActivityHint:2][FragLength:2][FragNum:2][AuthProto:1][SerialLow:1]
  (= 80 bytes total header)

NDR payload for PNIO Connect carries a sequence of PNIO CM blocks.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

from .constants import (
    RPC_PKT_REQUEST, RPC_PKT_RESPONSE,
    RPC_PFC_FIRST_FRAG, RPC_PFC_LAST_FRAG, RPC_PFC_NO_FACK,
    RPC_DREP_LITTLE_ENDIAN,
    PNIO_DEVICE_INTERFACE_UUID,
    PNIO_OP_CONNECT, PNIO_OP_RELEASE, PNIO_OP_CONTROL, PNIO_OP_READ,
    BLOCK_AR_REQ, BLOCK_AR_RES,
    BLOCK_IOCR_REQ, BLOCK_IOCR_RES,
    BLOCK_ALARM_CR_REQ, BLOCK_ALARM_CR_RES,
    BLOCK_EXPECTED_SUBMOD, BLOCK_MODULE_DIFF,
    BLOCK_AR_SERVER, BLOCK_CONTROL_DATA,
    IOCR_INPUT, IOCR_OUTPUT,
    INPUT_DATA_SIZE, OUTPUT_DATA_SIZE,
    DEFAULT_INPUT_FRAME_ID, DEFAULT_OUTPUT_FRAME_ID,
    PNIO_API, VENDOR_ID, DEVICE_ID,
    FRAME_ID_ALARM_LOW,
)

logger = logging.getLogger(__name__)

_RPC_HEADER_SIZE = 80   # fixed DCE/RPC header length in bytes


@dataclass
class IOCRInfo:
    """Negotiated parameters for one IO Communication Relationship."""
    iocr_type:      int   # IOCR_INPUT or IOCR_OUTPUT
    frame_id:       int   # Ethernet frame ID for cyclic data
    data_length:    int   # process data length (bytes, excluding IOPS/IOCS)
    send_clock:     int   # SendClockFactor (31.25 µs units)
    reduction:      int   # ReductionRatio
    phase:          int   # Phase
    udp_rt_port:    int   # UDP RT port (0 for RT class 1)


@dataclass
class ApplicationRelationship:
    """State for one open Application Relationship."""
    ar_uuid:            bytes          # 16-byte AR UUID
    session_key:        int
    controller_mac:     str            # CMInitiator MAC address
    controller_ip:      str            # IP from which Connect arrived
    controller_udp_port: int           # InitiatorUDPRTPort
    activity_uuid:      bytes          # 16-byte DCE/RPC activity UUID
    sequence_num:       int
    input_iocr:         Optional[IOCRInfo] = None
    output_iocr:        Optional[IOCRInfo] = None
    alarm_frame_id:     int = FRAME_ID_ALARM_LOW
    established:        bool = False


class CMHandler:
    """
    Handles PNIO CM (Connection Management) DCE/RPC messages over UDP.

    Usage::

        cm = CMHandler(mac_address="AA:BB:CC:DD:EE:FF",
                       ip_address="192.168.0.10",
                       station_name="qc-inspection-sys")
        cm.on_ar_established = lambda ar: ...
        cm.on_ar_released    = lambda ar: ...

        # In the UDP receive loop:
        response = cm.handle_udp(data, src_addr)
        if response:
            sock.sendto(response, src_addr)
    """

    def __init__(
        self,
        mac_address:  str,
        ip_address:   str,
        station_name: str,
    ) -> None:
        self._mac         = mac_address
        self._ip          = ip_address
        self._name        = station_name

        # Boot time written into DCE/RPC responses (seconds since epoch, 32-bit)
        self._boot_time   = int(time.time()) & 0xFFFFFFFF

        # Currently open ARs (keyed by AR UUID bytes)
        self._ars: dict[bytes, ApplicationRelationship] = {}

        # Callbacks
        self.on_ar_established: Optional[Callable[[ApplicationRelationship], None]] = None
        self.on_ar_released:    Optional[Callable[[ApplicationRelationship], None]] = None

    # =========================================================================
    # Public
    # =========================================================================

    @property
    def active_ar(self) -> Optional[ApplicationRelationship]:
        """Return the first established AR, or None."""
        for ar in self._ars.values():
            if ar.established:
                return ar
        return None

    def handle_udp(self, data: bytes, src_ip: str) -> Optional[bytes]:
        """
        Process one incoming UDP datagram from the controller.

        Returns the response bytes to send back, or None.
        """
        if len(data) < _RPC_HEADER_SIZE:
            return None

        # Parse DCE/RPC header
        version      = data[0]
        pkt_type     = data[1]
        flags        = data[2]
        data_rep     = data[4:8]
        object_uuid  = data[9:25]
        iface_uuid   = data[25:41]
        activity_uuid = data[41:57]
        seq_num      = struct.unpack_from("<I", data, 65)[0]
        op_num       = struct.unpack_from("<H", data, 69)[0]
        frag_length  = struct.unpack_from("<H", data, 73)[0]
        serial_low   = data[79]

        if version != 4 or pkt_type != RPC_PKT_REQUEST:
            return None

        ndr = data[_RPC_HEADER_SIZE:]

        logger.debug(
            "PNIO CM UDP | op=%d seq=%d src=%s", op_num, seq_num, src_ip
        )

        if op_num == PNIO_OP_CONNECT:
            return self._handle_connect(
                ndr, activity_uuid, seq_num, serial_low, src_ip, data_rep
            )

        if op_num == PNIO_OP_CONTROL:
            return self._handle_control(
                ndr, activity_uuid, seq_num, serial_low, data_rep
            )

        if op_num == PNIO_OP_RELEASE:
            return self._handle_release(
                ndr, activity_uuid, seq_num, serial_low, data_rep
            )

        if op_num == PNIO_OP_READ:
            # Return an empty read response (no records to provide)
            return self._rpc_response(
                activity_uuid, seq_num, serial_low, op_num,
                self._build_read_res_empty(), data_rep
            )

        return None

    # =========================================================================
    # Connect
    # =========================================================================

    def _handle_connect(
        self,
        ndr:           bytes,
        activity_uuid: bytes,
        seq_num:       int,
        serial_low:    int,
        src_ip:        str,
        data_rep:      bytes,
    ) -> Optional[bytes]:
        """Parse Connect.req and build Connect.res."""
        ar, iocr_list, alarm_frame_id = self._parse_connect_req(ndr)
        if ar is None:
            logger.warning("PNIO CM: could not parse Connect.req")
            return None

        ar.activity_uuid  = activity_uuid
        ar.sequence_num   = seq_num
        ar.controller_ip  = src_ip
        ar.alarm_frame_id = alarm_frame_id

        # Assign IOCRs
        for iocr in iocr_list:
            if iocr.iocr_type == IOCR_INPUT:
                ar.input_iocr = iocr
            elif iocr.iocr_type == IOCR_OUTPUT:
                ar.output_iocr = iocr

        # Fall back to defaults if controller did not send both CRs
        if ar.input_iocr is None:
            ar.input_iocr = IOCRInfo(
                iocr_type=IOCR_INPUT, frame_id=DEFAULT_INPUT_FRAME_ID,
                data_length=INPUT_DATA_SIZE, send_clock=32,
                reduction=1, phase=1, udp_rt_port=0,
            )
        if ar.output_iocr is None:
            ar.output_iocr = IOCRInfo(
                iocr_type=IOCR_OUTPUT, frame_id=DEFAULT_OUTPUT_FRAME_ID,
                data_length=OUTPUT_DATA_SIZE, send_clock=32,
                reduction=1, phase=1, udp_rt_port=0,
            )

        self._ars[ar.ar_uuid] = ar
        logger.info(
            "PNIO CM Connect.req | AR=%s in_fid=0x%04X out_fid=0x%04X src=%s",
            ar.ar_uuid.hex(), ar.input_iocr.frame_id,
            ar.output_iocr.frame_id, src_ip,
        )

        ndr_res = self._build_connect_res(ar)
        return self._rpc_response(activity_uuid, seq_num, serial_low, PNIO_OP_CONNECT, ndr_res, data_rep)

    def _parse_connect_req(
        self, ndr: bytes
    ) -> tuple[Optional[ApplicationRelationship], list[IOCRInfo], int]:
        """
        Parse the NDR payload of a Connect.req.

        Returns (AR, list-of-IOCRInfo, alarm_frame_id).
        """
        ar           = None
        iocrs:       list[IOCRInfo] = []
        alarm_fid    = FRAME_ID_ALARM_LOW
        offset       = 0

        # Skip 4-byte NDR header (ArgLength or padding)
        if len(ndr) < 4:
            return None, [], alarm_fid
        offset = 4

        while offset + 4 <= len(ndr):
            block_type = struct.unpack_from("!H", ndr, offset)[0]
            block_len  = struct.unpack_from("!H", ndr, offset + 2)[0]
            # block_len includes BlockVersionHigh and BlockVersionLow (2 bytes)
            # so actual value field starts at offset + 4 + 2 = offset + 6
            block_data = ndr[offset + 6: offset + 4 + block_len]
            next_offset = offset + 4 + block_len
            # Blocks are word-aligned
            if (4 + block_len) % 2 != 0:
                next_offset += 1

            if block_type == BLOCK_AR_REQ:
                ar = self._parse_ar_block(block_data)

            elif block_type == BLOCK_IOCR_REQ:
                iocr = self._parse_iocr_block(block_data)
                if iocr:
                    iocrs.append(iocr)

            elif block_type == BLOCK_ALARM_CR_REQ:
                # Alarm CR: [AlarmCRType:2][LT:2][Properties:4][RTA_Timeout:2]
                # [RTA_Retries:2][LocalAlarmRef:2][MaxAlarmDataLength:2]
                # [AlarmCRTagHeaderHigh:2][AlarmCRTagHeaderLow:2]
                if len(block_data) >= 2:
                    # Frame ID for alarms is carried in the tag headers
                    # but we derive it from standard positions
                    pass

            offset = next_offset

        return ar, iocrs, alarm_fid

    def _parse_ar_block(self, data: bytes) -> Optional[ApplicationRelationship]:
        """Parse ARBlockReq payload (after block header)."""
        if len(data) < 54:
            return None
        # [ARType:2][ARUUID:16][SessionKey:2][CMInitiatorMACAdd:6]
        # [CMInitiatorObjectUUID:16][Properties:4][CMInitiatorActivityTimeoutFactor:2]
        # [InitiatorUDPRTPort:2][StationNameLength:2][StationName:N]
        ar_type    = struct.unpack_from("!H", data, 0)[0]
        ar_uuid    = data[2:18]
        session_key = struct.unpack_from("!H", data, 18)[0]
        mac_raw    = data[20:26]
        ctrl_mac   = ":".join(f"{b:02X}" for b in mac_raw)
        # skip CMInitiatorObjectUUID (16 bytes at offset 26)
        udp_port   = struct.unpack_from("!H", data, 46)[0]

        return ApplicationRelationship(
            ar_uuid=ar_uuid,
            session_key=session_key,
            controller_mac=ctrl_mac,
            controller_ip="",
            controller_udp_port=udp_port,
            activity_uuid=b"\x00" * 16,
            sequence_num=0,
        )

    def _parse_iocr_block(self, data: bytes) -> Optional[IOCRInfo]:
        """Parse IOCRBlockReq payload (after block header)."""
        if len(data) < 24:
            return None
        # [IOCRType:2][IOCRReference:2][LT:2][IOCRProperties:4][DataLength:2]
        # [FrameID:2][SendClockFactor:2][ReductionRatio:2][Phase:2][Sequence:2]
        # [FrameSendOffset:4][WatchdogFactor:2][DataHoldFactor:2]
        # [IOCRTagHeader:2][MulticastMACAdd:6] then APIList...
        iocr_type   = struct.unpack_from("!H", data, 0)[0]
        data_length = struct.unpack_from("!H", data, 8)[0]
        frame_id    = struct.unpack_from("!H", data, 10)[0]
        send_clock  = struct.unpack_from("!H", data, 12)[0]
        reduction   = struct.unpack_from("!H", data, 14)[0]
        phase       = struct.unpack_from("!H", data, 16)[0]

        return IOCRInfo(
            iocr_type=iocr_type,
            frame_id=frame_id,
            data_length=data_length,
            send_clock=send_clock,
            reduction=reduction,
            phase=phase,
            udp_rt_port=0,
        )

    def _build_connect_res(self, ar: ApplicationRelationship) -> bytes:
        """Build the NDR payload for Connect.res."""
        mac_bytes = bytes(int(x, 16) for x in self._mac.split(":"))
        ip_bytes  = _ip_to_bytes(self._ip)

        # ARBlockRes
        ar_res = _make_cm_block(
            BLOCK_AR_RES,
            struct.pack("!H", ar.session_key) + mac_bytes + ip_bytes,
        )

        # IOCRBlockRes for input
        in_iocr = ar.input_iocr
        iocr_in_res = _make_cm_block(
            BLOCK_IOCR_RES,
            struct.pack(
                "!HHHH",
                IOCR_INPUT,          # IOCRType
                1,                   # IOCRReference
                in_iocr.frame_id,    # FrameID confirmed
                0,                   # Reserved
            ),
        )

        # IOCRBlockRes for output
        out_iocr = ar.output_iocr
        iocr_out_res = _make_cm_block(
            BLOCK_IOCR_RES,
            struct.pack(
                "!HHHH",
                IOCR_OUTPUT,
                2,
                out_iocr.frame_id,
                0,
            ),
        )

        # AlarmCRBlockRes
        alarm_res = _make_cm_block(
            BLOCK_ALARM_CR_RES,
            struct.pack("!HH", 1, ar.alarm_frame_id),
        )

        # ModuleDiffBlock — report all submodules as OK (Proper state)
        # [NumberOfAPIs:2][API:4][NumberOfModules:2][SlotNumber:2][ModuleIdentNumber:4]
        # [ModuleState:2][NumberOfSubmodules:2][SubslotNumber:2][SubmoduleIdentNumber:4][SubmoduleState:2]
        mod_diff_payload = struct.pack(
            "!HIHH IHH IH",
            1,              # NumberOfAPIs
            PNIO_API,       # API
            1,              # NumberOfModules
            1,              # SlotNumber
            0x00000002,     # ModuleIdentNumber (from GSDML)
            0x0000,         # ModuleState: OK
            1,              # NumberOfSubmodules
            1,              # SubslotNumber
            0x00000001,     # SubmoduleIdentNumber (from GSDML)
            0x0000,         # SubmoduleState: OK (no substitution, no wrong)
        )
        mod_diff = _make_cm_block(BLOCK_MODULE_DIFF, mod_diff_payload)

        # AR server block (device identity)
        name_bytes  = self._name.encode("ascii")
        name_padded = name_bytes if len(name_bytes) % 2 == 0 else name_bytes + b"\x00"
        ar_server_payload = (
            struct.pack("!HH", VENDOR_ID, DEVICE_ID)
            + b"\x00" * 6           # CMResponderMacAdd placeholder (not in this block)
            + mac_bytes
            + struct.pack("!H", len(name_bytes))
            + name_padded
        )
        ar_server = _make_cm_block(BLOCK_AR_SERVER, ar_server_payload)

        ndr_body = ar_res + iocr_in_res + iocr_out_res + alarm_res + mod_diff + ar_server

        # NDR header: ArgLength(4) + padding(4)
        return struct.pack("!II", len(ndr_body), 0) + ndr_body

    # =========================================================================
    # Control (ApplicationReady)
    # =========================================================================

    def _handle_control(
        self,
        ndr:           bytes,
        activity_uuid: bytes,
        seq_num:       int,
        serial_low:    int,
        data_rep:      bytes,
    ) -> Optional[bytes]:
        """
        Handle ControlBlockConnect (ApplicationReady or PrmEnd).
        After receiving ApplicationReady, mark the AR as established.
        """
        # Find AR from activity UUID
        ar = self._find_ar_by_activity(activity_uuid)
        if ar is None:
            logger.warning("PNIO CM Control: no AR found for activity %s", activity_uuid.hex())
            return None

        # Parse ControlBlock: [BlockType:2][BlockLen:2][Ver:2][Padding:2][ARUUID:16]
        # [SessionKey:2][AlarmSeq:2][ControlCommand:2][ControlBlockProperties:2]
        # ControlCommand bit 0x0001 = Done (ApplicationReady)
        if len(ndr) >= 32:
            control_cmd = struct.unpack_from("!H", ndr, 28)[0]
            logger.info(
                "PNIO CM Control | cmd=0x%04X AR=%s", control_cmd, ar.ar_uuid.hex()
            )
            if control_cmd & 0x0001:   # ApplicationReady
                ar.established = True
                logger.info(
                    "PNIO IO AR established | in_fid=0x%04X out_fid=0x%04X",
                    ar.input_iocr.frame_id if ar.input_iocr else 0,
                    ar.output_iocr.frame_id if ar.output_iocr else 0,
                )
                if self.on_ar_established:
                    self.on_ar_established(ar)

        # ControlBlockRes response
        ndr_res = _make_cm_block(
            BLOCK_CONTROL_DATA,
            struct.pack("!HH", ar.session_key, 0x0008),  # ControlCommand: Done
        )
        return self._rpc_response(
            activity_uuid, seq_num, serial_low, PNIO_OP_CONTROL,
            struct.pack("!II", len(ndr_res), 0) + ndr_res, data_rep,
        )

    # =========================================================================
    # Release
    # =========================================================================

    def _handle_release(
        self,
        ndr:           bytes,
        activity_uuid: bytes,
        seq_num:       int,
        serial_low:    int,
        data_rep:      bytes,
    ) -> Optional[bytes]:
        """Handle Release.req — tear down the AR."""
        ar = self._find_ar_by_activity(activity_uuid)
        if ar:
            logger.info("PNIO CM Release | AR=%s", ar.ar_uuid.hex())
            ar.established = False
            del self._ars[ar.ar_uuid]
            if self.on_ar_released:
                self.on_ar_released(ar)

        ndr_res = struct.pack("!II", 0, 0)
        return self._rpc_response(
            activity_uuid, seq_num, serial_low, PNIO_OP_RELEASE, ndr_res, data_rep
        )

    # =========================================================================
    # Read (empty response)
    # =========================================================================

    @staticmethod
    def _build_read_res_empty() -> bytes:
        # Minimal read response: ArgLength=0
        return struct.pack("!II", 0, 0)

    # =========================================================================
    # DCE/RPC response builder
    # =========================================================================

    def _rpc_response(
        self,
        activity_uuid: bytes,
        seq_num:       int,
        serial_low:    int,
        op_num:        int,
        ndr_payload:   bytes,
        data_rep:      bytes,
    ) -> bytes:
        """Wrap NDR payload in a DCE/RPC response header."""
        frag_len = _RPC_HEADER_SIZE + len(ndr_payload)
        flags    = RPC_PFC_FIRST_FRAG | RPC_PFC_LAST_FRAG

        # Object UUID = all zeros (not used)
        object_uuid = b"\x00" * 16

        header = struct.pack(
            "!BB BB 4s B 16s 16s 16s I I I HHH HHB B",
            4,                          # Version
            RPC_PKT_RESPONSE,           # PacketType
            flags,                      # PFCFlags
            0,                          # PFCFlags2
        )
        # Build manually (struct format above is illustrative; field sizes vary)
        # Use explicit byte packing for clarity:
        hdr = bytearray(_RPC_HEADER_SIZE)
        hdr[0]  = 4                      # Version
        hdr[1]  = RPC_PKT_RESPONSE
        hdr[2]  = flags
        hdr[3]  = 0                      # Flags2
        hdr[4:8] = data_rep              # DataRep (echo back)
        hdr[8]  = 0                      # SerialHigh
        # ObjectUUID [9:25] = zeros
        # InterfaceUUID [25:41] = device interface UUID
        hdr[25:41] = PNIO_DEVICE_INTERFACE_UUID
        # ActivityUUID [41:57]
        hdr[41:57] = activity_uuid
        # ServerBootTime [57:61]
        struct.pack_into("<I", hdr, 57, self._boot_time)
        # InterfaceVersion [61:65] = 1.0
        struct.pack_into("<HH", hdr, 61, 1, 0)
        # SequenceNum [65:69]
        struct.pack_into("<I", hdr, 65, seq_num)
        # OpNum [69:71]
        struct.pack_into("<H", hdr, 69, op_num)
        # InterfaceHint [71:73]
        struct.pack_into("<H", hdr, 71, 0xFFFF)
        # ActivityHint [73:75]
        struct.pack_into("<H", hdr, 73, 0xFFFF)
        # FragLength [75:77] (wait, let me recheck offsets)

        # Correct DCE/RPC header layout (80 bytes):
        # 0:  Version(1)
        # 1:  PacketType(1)
        # 2:  Flags(1)
        # 3:  Flags2(1)
        # 4:  DataRep(4)
        # 8:  SerialHigh(1)
        # 9:  ObjectUUID(16)      → bytes 9-24
        # 25: InterfaceUUID(16)   → bytes 25-40
        # 41: ActivityUUID(16)    → bytes 41-56
        # 57: ServerBootTime(4)   → bytes 57-60
        # 61: InterfaceVersion(4) → bytes 61-64 (major:2, minor:2)
        # 65: SequenceNum(4)      → bytes 65-68
        # 69: OpNum(2)            → bytes 69-70
        # 71: InterfaceHint(2)    → bytes 71-72
        # 73: ActivityHint(2)     → bytes 73-74
        # 75: FragLength(2)       → bytes 75-76
        # 77: FragNum(2)          → bytes 77-78
        # 79: AuthProto(1) + SerialLow(1)  ← but that's only 80 bytes if SerialLow at 79
        # Actually standard DCE/RPC CL header is 80 bytes:
        # Let me rebuild correctly:
        hdr = bytearray(80)
        hdr[0]  = 4                       # Version = 4 (connection-less)
        hdr[1]  = RPC_PKT_RESPONSE
        hdr[2]  = RPC_PFC_FIRST_FRAG | RPC_PFC_LAST_FRAG
        hdr[3]  = 0
        hdr[4:8] = data_rep
        hdr[8]  = 0                       # SerialHigh
        # ObjectUUID 9-24: zeros
        hdr[25:41] = PNIO_DEVICE_INTERFACE_UUID
        hdr[41:57] = activity_uuid
        struct.pack_into("<I",  hdr, 57, self._boot_time)
        struct.pack_into("<HH", hdr, 61, 1, 0)           # InterfaceVersion 1.0
        struct.pack_into("<I",  hdr, 65, seq_num)
        struct.pack_into("<H",  hdr, 69, op_num)
        struct.pack_into("<H",  hdr, 71, 0xFFFF)         # InterfaceHint
        struct.pack_into("<H",  hdr, 73, 0xFFFF)         # ActivityHint
        frag_len = 80 + len(ndr_payload)
        struct.pack_into("<H",  hdr, 75, frag_len)       # FragLength
        struct.pack_into("<H",  hdr, 77, 0)              # FragNum
        hdr[79] = serial_low                             # SerialLow

        return bytes(hdr) + ndr_payload

    # =========================================================================
    # Helpers
    # =========================================================================

    def _find_ar_by_activity(self, activity_uuid: bytes) -> Optional[ApplicationRelationship]:
        for ar in self._ars.values():
            if ar.activity_uuid == activity_uuid:
                return ar
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cm_block(block_type: int, data: bytes) -> bytes:
    """
    Pack a PNIO CM block:
    [BlockType:2][BlockLength:2][BlockVersionHigh:1][BlockVersionLow:1][Data...]

    BlockLength counts from BlockVersionHigh onwards.
    """
    # version bytes + data
    payload     = bytes([1, 0]) + data    # BlockVersionHigh=1, Low=0
    block_length = len(payload)
    header = struct.pack("!HH", block_type, block_length)
    # Pad to word boundary
    pad = b"\x00" if block_length % 2 != 0 else b""
    return header + payload + pad


def _ip_to_bytes(ip: str) -> bytes:
    import socket
    return socket.inet_aton(ip)
