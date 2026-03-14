"""
services/profinet_io/dcp.py — PROFINET DCP (Discovery and Configuration Protocol).

DCP is carried in raw Ethernet frames with EtherType 0x8892.  It handles:
  1. Device discovery  — controller multicasts an Identify request; device
                         responds unicast with its identity and IP info.
  2. Station naming    — controller writes the NameOfStation; device stores it.
  3. IP configuration  — controller writes IP/subnet/gateway; device applies it.

Frame structure (all big-endian):
  [FrameID:2][ServiceID:1][ServiceType:1][Xid:4][ResponseDelay:2]
  [DCPDataLength:2][Block0][Block1]…

Each Block:
  [Option:1][Suboption:1][DCPBlockLength:2][Value...][Pad:0or1]
"""

from __future__ import annotations

import logging
import socket
import struct
from typing import Optional

from .constants import (
    FRAME_ID_DCP_IDENTIFY_REQ, FRAME_ID_DCP_IDENTIFY_RES, FRAME_ID_DCP_GETSET,
    DCP_SVC_IDENTIFY, DCP_SVC_GET, DCP_SVC_SET,
    DCP_TYPE_REQUEST, DCP_TYPE_SUCCESS,
    DCP_OPT_IP, DCP_OPT_DEVICE, DCP_OPT_CONTROL, DCP_OPT_ALL,
    DCP_SUBOPT_IP_MAC, DCP_SUBOPT_IP_PARAM,
    DCP_SUBOPT_DEV_TYPE, DCP_SUBOPT_DEV_NAME, DCP_SUBOPT_DEV_ID,
    DCP_SUBOPT_DEV_ROLE, DCP_SUBOPT_DEV_OPTIONS,
    DCP_SUBOPT_CTRL_RESPONSE, DCP_SUBOPT_CTRL_FACTORY,
    DCP_ROLE_IO_DEVICE, DCP_ERR_OK, DCP_ERR_NOT_SUPPORTED,
    VENDOR_ID, DEVICE_ID, STATION_TYPE,
)

logger = logging.getLogger(__name__)

# DCP PDU header length (FrameID + ServiceID + ServiceType + Xid + Delay + Length)
_DCP_HEADER = 10   # bytes 0-9 are the header before any blocks


class DCPHandler:
    """
    Handles incoming DCP frames and builds response frames.

    The owning ProfinetService feeds raw Ethernet payload (starting at the
    FrameID byte) to ``handle_frame()`` and sends back any non-None result
    as a unicast Ethernet frame to the controller's MAC address.

    State kept here is simple — just the three configurable parameters that
    DCP allows the controller to write: station name, IP address, subnet,
    and gateway.  These are written to the in-process state only; callers
    should persist / apply them as needed.
    """

    def __init__(
        self,
        station_name: str,
        mac_address:  str,
        ip_address:   str,
        subnet_mask:  str,
        gateway:      str,
    ) -> None:
        self.station_name = station_name
        self.mac_address  = mac_address
        self.ip_address   = ip_address
        self.subnet_mask  = subnet_mask
        self.gateway      = gateway

        # Callbacks (set by ProfinetService)
        self.on_name_changed: Optional[callable] = None
        self.on_ip_changed:   Optional[callable] = None

    # =========================================================================
    # Public
    # =========================================================================

    def handle_frame(self, payload: bytes, src_mac: str) -> Optional[bytes]:
        """
        Process a raw DCP Ethernet payload (from FrameID byte onwards).

        Returns the response payload (from FrameID byte onwards) to be
        wrapped in an Ethernet frame and sent to ``src_mac``, or ``None``
        if no response is required.
        """
        if len(payload) < _DCP_HEADER:
            return None

        frame_id     = struct.unpack_from("!H", payload, 0)[0]
        service_id   = payload[2]
        service_type = payload[3]
        xid          = struct.unpack_from("!I", payload, 4)[0]
        # bytes 8-9: ResponseDelay (we ignore it for response timing here)
        data_length  = struct.unpack_from("!H", payload, 8)[0]

        if service_type != DCP_TYPE_REQUEST:
            return None   # only process requests

        if frame_id == FRAME_ID_DCP_IDENTIFY_REQ and service_id == DCP_SVC_IDENTIFY:
            logger.debug("DCP Identify request from %s xid=0x%08X", src_mac, xid)
            return self._build_identify_response(xid)

        if frame_id == FRAME_ID_DCP_GETSET:
            if service_id == DCP_SVC_GET:
                logger.debug("DCP Get request from %s xid=0x%08X", src_mac, xid)
                blocks = payload[_DCP_HEADER: _DCP_HEADER + data_length]
                return self._handle_get(xid, blocks)

            if service_id == DCP_SVC_SET:
                logger.debug("DCP Set request from %s xid=0x%08X", src_mac, xid)
                blocks = payload[_DCP_HEADER: _DCP_HEADER + data_length]
                return self._handle_set(xid, blocks)

        return None

    # =========================================================================
    # Private — response builders
    # =========================================================================

    def _build_identify_response(self, xid: int) -> bytes:
        """Build a DCP IdentifyResponse PDU."""
        blocks = (
            self._block_ip_param()
            + self._block_station_type()
            + self._block_station_name()
            + self._block_device_id()
            + self._block_device_role()
            + self._block_device_options()
        )
        return self._dcp_header(
            frame_id=FRAME_ID_DCP_IDENTIFY_RES,
            service_id=DCP_SVC_IDENTIFY,
            service_type=DCP_TYPE_SUCCESS,
            xid=xid,
            response_delay=0,
            blocks=blocks,
        )

    def _handle_get(self, xid: int, blocks_data: bytes) -> Optional[bytes]:
        """Build a DCP GetResponse for the requested option/suboption pairs."""
        response_blocks = b""
        offset = 0
        while offset + 4 <= len(blocks_data):
            opt    = blocks_data[offset]
            subopt = blocks_data[offset + 1]
            offset += 2
            # In a Get request, option/suboption pairs have no length/value
            if (opt, subopt) == (DCP_OPT_IP, DCP_SUBOPT_IP_PARAM):
                response_blocks += self._block_ip_param()
            elif (opt, subopt) == (DCP_OPT_DEVICE, DCP_SUBOPT_DEV_NAME):
                response_blocks += self._block_station_name()
            elif (opt, subopt) == (DCP_OPT_DEVICE, DCP_SUBOPT_DEV_ID):
                response_blocks += self._block_device_id()
            elif (opt, subopt) == (DCP_OPT_DEVICE, DCP_SUBOPT_DEV_ROLE):
                response_blocks += self._block_device_role()
            elif (opt, subopt) == (DCP_OPT_ALL, 0xFF):
                response_blocks += (
                    self._block_ip_param()
                    + self._block_station_type()
                    + self._block_station_name()
                    + self._block_device_id()
                    + self._block_device_role()
                )
                break

        if not response_blocks:
            return None

        return self._dcp_header(
            frame_id=FRAME_ID_DCP_GETSET,
            service_id=DCP_SVC_GET,
            service_type=DCP_TYPE_SUCCESS,
            xid=xid,
            response_delay=0,
            blocks=response_blocks,
        )

    def _handle_set(self, xid: int, blocks_data: bytes) -> bytes:
        """
        Process a DCP SetRequest.  Apply station name and/or IP parameters.
        Return a SetResponse confirming each block.
        """
        response_blocks = b""
        offset = 0
        while offset + 4 <= len(blocks_data):
            opt     = blocks_data[offset]
            subopt  = blocks_data[offset + 1]
            blk_len = struct.unpack_from("!H", blocks_data, offset + 2)[0]
            value   = blocks_data[offset + 4: offset + 4 + blk_len]
            # advance past block (padded to even length)
            offset += 4 + blk_len + (blk_len % 2)

            err = DCP_ERR_OK

            if opt == DCP_OPT_DEVICE and subopt == DCP_SUBOPT_DEV_NAME:
                new_name = value.decode("ascii", errors="replace").rstrip("\x00")
                logger.info("DCP Set: station name → '%s'", new_name)
                self.station_name = new_name
                if self.on_name_changed:
                    self.on_name_changed(new_name)

            elif opt == DCP_OPT_IP and subopt == DCP_SUBOPT_IP_PARAM:
                # value = [BlockInfo:2][IP:4][Subnet:4][Gateway:4]
                if len(value) >= 14:
                    new_ip  = socket.inet_ntoa(value[2:6])
                    new_sub = socket.inet_ntoa(value[6:10])
                    new_gw  = socket.inet_ntoa(value[10:14])
                    logger.info(
                        "DCP Set: IP=%s subnet=%s gateway=%s", new_ip, new_sub, new_gw
                    )
                    self.ip_address  = new_ip
                    self.subnet_mask = new_sub
                    self.gateway     = new_gw
                    if self.on_ip_changed:
                        self.on_ip_changed(new_ip, new_sub, new_gw)
                else:
                    err = DCP_ERR_NOT_SUPPORTED

            elif opt == DCP_OPT_CONTROL and subopt == DCP_SUBOPT_CTRL_FACTORY:
                logger.info("DCP Set: factory reset requested")
                # For this implementation we acknowledge but do not reset

            else:
                logger.debug("DCP Set: unsupported option=0x%02X sub=0x%02X", opt, subopt)
                err = DCP_ERR_NOT_SUPPORTED

            # Control Response block: [opt][subopt][len=3][err_opt][err_sub][err_code]
            ctrl_data = bytes([opt, subopt, err])
            response_blocks += struct.pack("!BBH", DCP_OPT_CONTROL, DCP_SUBOPT_CTRL_RESPONSE, 3)
            response_blocks += ctrl_data + b"\x00"   # pad to even

        return self._dcp_header(
            frame_id=FRAME_ID_DCP_GETSET,
            service_id=DCP_SVC_SET,
            service_type=DCP_TYPE_SUCCESS,
            xid=xid,
            response_delay=0,
            blocks=response_blocks,
        )

    # =========================================================================
    # Block builders (each returns bytes including option/subopt/length/value)
    # =========================================================================

    def _block_ip_param(self) -> bytes:
        """Block: Option=0x01, Suboption=0x02 — IP address parameters."""
        # BlockInfo: 0x0001 = IP set manually
        block_info = 0x0001
        data = struct.pack("!H", block_info)
        data += socket.inet_aton(self.ip_address)
        data += socket.inet_aton(self.subnet_mask)
        data += socket.inet_aton(self.gateway)
        return _make_block(DCP_OPT_IP, DCP_SUBOPT_IP_PARAM, data)

    def _block_station_type(self) -> bytes:
        """Block: Option=0x02, Suboption=0x01 — ManufacturerSpecificValue (station type)."""
        return _make_block(DCP_OPT_DEVICE, DCP_SUBOPT_DEV_TYPE, STATION_TYPE.encode("ascii"))

    def _block_station_name(self) -> bytes:
        """Block: Option=0x02, Suboption=0x02 — NameOfStation."""
        return _make_block(DCP_OPT_DEVICE, DCP_SUBOPT_DEV_NAME, self.station_name.encode("ascii"))

    def _block_device_id(self) -> bytes:
        """Block: Option=0x02, Suboption=0x04 — VendorID + DeviceID."""
        data = struct.pack("!HH", VENDOR_ID, DEVICE_ID)
        return _make_block(DCP_OPT_DEVICE, DCP_SUBOPT_DEV_ID, data)

    def _block_device_role(self) -> bytes:
        """Block: Option=0x02, Suboption=0x05 — DeviceRoleDetails (IO Device)."""
        # DeviceRole[1] + Reserved[1]
        data = bytes([DCP_ROLE_IO_DEVICE, 0x00])
        return _make_block(DCP_OPT_DEVICE, DCP_SUBOPT_DEV_ROLE, data)

    def _block_device_options(self) -> bytes:
        """Block: Option=0x02, Suboption=0x06 — supported DCP options."""
        # List of (option, suboption) pairs that this device supports
        pairs = [
            (DCP_OPT_IP,     DCP_SUBOPT_IP_PARAM),
            (DCP_OPT_DEVICE, DCP_SUBOPT_DEV_NAME),
            (DCP_OPT_DEVICE, DCP_SUBOPT_DEV_ID),
            (DCP_OPT_DEVICE, DCP_SUBOPT_DEV_ROLE),
            (DCP_OPT_DEVICE, DCP_SUBOPT_DEV_TYPE),
            (DCP_OPT_CONTROL, DCP_SUBOPT_CTRL_FACTORY),
        ]
        data = b"".join(bytes([o, s]) for o, s in pairs)
        return _make_block(DCP_OPT_DEVICE, DCP_SUBOPT_DEV_OPTIONS, data)

    # =========================================================================
    # Header builder
    # =========================================================================

    @staticmethod
    def _dcp_header(
        frame_id: int,
        service_id: int,
        service_type: int,
        xid: int,
        response_delay: int,
        blocks: bytes,
    ) -> bytes:
        header = struct.pack(
            "!HBBIHH",
            frame_id,
            service_id,
            service_type,
            xid,
            response_delay,
            len(blocks),
        )
        return header + blocks


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_block(option: int, suboption: int, data: bytes) -> bytes:
    """Pack a DCP block: [option][suboption][length:2][data][pad]."""
    length = len(data)
    header = struct.pack("!BBH", option, suboption, length)
    pad    = b"\x00" if length % 2 != 0 else b""
    return header + data + pad
