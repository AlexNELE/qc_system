"""
services/profinet_io/constants.py — PROFINET IO protocol constants.

Sources: IEC 61158-6-10 (PROFINET IO), IEC 61784-2, PROFIBUS & PROFINET
International (PI) documentation.
"""

# ---------------------------------------------------------------------------
# Ethernet
# ---------------------------------------------------------------------------
PNIO_ETHERTYPE          = 0x8892   # EtherType for all PROFINET RT frames
LLDP_ETHERTYPE          = 0x88CC   # EtherType for LLDP
DCP_MULTICAST_MAC       = "01:0E:CF:00:00:00"   # DCP Identify multicast
DCP_MULTICAST_MAC_HELLO = "01:0E:CF:00:00:01"   # DCP Hello multicast

# ---------------------------------------------------------------------------
# DCP — Frame IDs (within EtherType 0x8892)
# ---------------------------------------------------------------------------
FRAME_ID_DCP_HELLO        = 0xFEFE   # device → controller announcement
FRAME_ID_DCP_GETSET       = 0xFEFF   # Get / Set unicast
FRAME_ID_DCP_IDENTIFY_REQ = 0xFEFC   # controller → devices multicast/unicast
FRAME_ID_DCP_IDENTIFY_RES = 0xFEFD   # device → controller unicast

# ---------------------------------------------------------------------------
# DCP — Service IDs
# ---------------------------------------------------------------------------
DCP_SVC_GET      = 0x03
DCP_SVC_SET      = 0x04
DCP_SVC_IDENTIFY = 0x05
DCP_SVC_HELLO    = 0x06

# DCP — Service Type
DCP_TYPE_REQUEST  = 0x00
DCP_TYPE_SUCCESS  = 0x01
DCP_TYPE_ERROR    = 0x05

# ---------------------------------------------------------------------------
# DCP — Block Options and Suboptions
# ---------------------------------------------------------------------------
DCP_OPT_IP      = 0x01
DCP_OPT_DEVICE  = 0x02
DCP_OPT_DHCP    = 0x03
DCP_OPT_CONTROL = 0x05
DCP_OPT_ALL     = 0xFF

# IP suboptions
DCP_SUBOPT_IP_MAC   = 0x01
DCP_SUBOPT_IP_PARAM = 0x02   # IP address, subnet, gateway

# Device suboptions
DCP_SUBOPT_DEV_TYPE    = 0x01   # ManufacturerSpecificValue (station type)
DCP_SUBOPT_DEV_NAME    = 0x02   # NameOfStation
DCP_SUBOPT_DEV_ID      = 0x04   # VendorID + DeviceID
DCP_SUBOPT_DEV_ROLE    = 0x05   # DeviceRoleDetails
DCP_SUBOPT_DEV_OPTIONS = 0x06   # DeviceOptions list

# Control suboptions
DCP_SUBOPT_CTRL_START    = 0x01
DCP_SUBOPT_CTRL_END      = 0x02
DCP_SUBOPT_CTRL_SIGNAL   = 0x03
DCP_SUBOPT_CTRL_RESPONSE = 0x04
DCP_SUBOPT_CTRL_FACTORY  = 0x05

# DCP Device Roles (bit mask for DEV_ROLE block)
DCP_ROLE_IO_DEVICE     = 0x02   # bit 1

# DCP response error codes (in Set/Response blocks)
DCP_ERR_OK            = 0x00
DCP_ERR_OPTION        = 0x01
DCP_ERR_SUBOPTION     = 0x02
DCP_ERR_NOT_SUPPORTED = 0x03
DCP_ERR_NOT_SET       = 0x04

# ---------------------------------------------------------------------------
# PNIO CM — Connection Management via DCE/RPC over UDP
# ---------------------------------------------------------------------------
PNIO_CM_UDP_PORT = 34964   # 0x88B4 — device listens on this port

# DCE/RPC packet types
RPC_PKT_REQUEST   = 0x00
RPC_PKT_PING      = 0x01
RPC_PKT_RESPONSE  = 0x02
RPC_PKT_FAULT     = 0x03
RPC_PKT_WORKING   = 0x04
RPC_PKT_REJECT    = 0x06
RPC_PKT_ACK       = 0x07
RPC_PKT_FACK      = 0x09

# DCE/RPC flags (PFC_FLAGS in header byte 3)
RPC_PFC_FIRST_FRAG = 0x04
RPC_PFC_LAST_FRAG  = 0x08
RPC_PFC_NO_FACK    = 0x20

# DCE/RPC data representation (little-endian Intel byte order)
RPC_DREP_LITTLE_ENDIAN = bytes([0x10, 0x00, 0x00, 0x00])

# PNIO CM Interface UUID — IO Device side
# DEA00002-6C97-11D1-8271-00A02442DF7D (Microsoft GUID little-endian encoding)
PNIO_DEVICE_INTERFACE_UUID = bytes([
    0x02, 0x00, 0xA0, 0xDE,   # DEA00002 → stored LE: 02 00 A0 DE
    0x97, 0x6C,               # 6C97     → stored LE: 97 6C
    0xD1, 0x11,               # 11D1     → stored BE: D1 11
    0x82, 0x71,               # 8271     → stored BE: 82 71
    0x00, 0xA0, 0x24, 0x42, 0xDF, 0x7D,  # 00A02442DF7D
])

# PNIO CM Operations (OpNum field in DCE/RPC header)
PNIO_OP_CONNECT     = 0
PNIO_OP_RELEASE     = 1
PNIO_OP_READ        = 2
PNIO_OP_WRITE       = 3
PNIO_OP_CONTROL     = 4

# ---------------------------------------------------------------------------
# PNIO CM — Block Types (in NDR payload after DCE/RPC header)
# ---------------------------------------------------------------------------
BLOCK_AR_REQ            = 0x0101
BLOCK_AR_RES            = 0x8101
BLOCK_IOCR_REQ          = 0x0102
BLOCK_IOCR_RES          = 0x8102
BLOCK_ALARM_CR_REQ      = 0x0103
BLOCK_ALARM_CR_RES      = 0x8103
BLOCK_EXPECTED_SUBMOD   = 0x0104
BLOCK_MODULE_DIFF       = 0x0804
BLOCK_AR_SERVER         = 0x0600
BLOCK_IOCR_TREQ         = 0x0110
BLOCK_CONTROL_DATA      = 0x0110   # ControlBlockConnect / ControlBlockPlug

# IOCR Types
IOCR_INPUT  = 0x0001   # Device provides input  data to Controller
IOCR_OUTPUT = 0x0002   # Controller provides output data to Device

# AR Types
AR_TYPE_IO_CONTROLLER   = 0x0001
AR_TYPE_SUPERVISOR      = 0x0006

# ---------------------------------------------------------------------------
# RT Cyclic
# ---------------------------------------------------------------------------
# Default frame IDs (overridden by CM negotiation at runtime)
DEFAULT_INPUT_FRAME_ID  = 0x8001   # Device → Controller
DEFAULT_OUTPUT_FRAME_ID = 0x8000   # Controller → Device

# IO Provider / Consumer Status
IOPS_GOOD  = 0x80   # data is valid
IOCS_GOOD  = 0x80   # consumer running OK

# APDU Status
DATA_STATUS_VALID   = 0x35   # primary, data valid, provider running
TRANSFER_STATUS_OK  = 0x00

# Process data sizes (must match PLCService DB layout)
INPUT_DATA_SIZE  = 16   # bytes: PC → PLC (flags + padding + INTs + DINT)
OUTPUT_DATA_SIZE = 1    # bytes: PLC → PC (trigger + inhibit flags)

# PROFINET default API number
PNIO_API = 0x00000000

# Alarm frame IDs
FRAME_ID_ALARM_HIGH = 0xFC01
FRAME_ID_ALARM_LOW  = 0xFE01

# ---------------------------------------------------------------------------
# Device Identity (must match GSDML)
# ---------------------------------------------------------------------------
VENDOR_ID   = 0x0045
DEVICE_ID   = 0x0001
STATION_TYPE = "QC-Inspection-System"   # manufacturer-specific type string
