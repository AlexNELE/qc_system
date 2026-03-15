"""
generate_manual.py — Build the QC System User & Installation Manual as PDF.

Usage (standalone):
    python generate_manual.py

The output file is written to ``docs/QCSystem_Manual.pdf``.
This script is also called by the PyInstaller post-build step so that the
manual is always included in the distribution.

Dependencies: reportlab (already installed for report_service.py).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import Color, HexColor, black, white
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
    HRFlowable,
    ListFlowable,
    ListItem,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ---------------------------------------------------------------------------
# Brand colours
# ---------------------------------------------------------------------------
_BRAND_DARK   = HexColor("#1C1C1E")
_BRAND_BLUE   = HexColor("#0A84FF")
_BRAND_SURFACE = HexColor("#2C2C2E")
_BRAND_GREEN  = HexColor("#30D158")
_BRAND_WARN   = HexColor("#FF9F0A")
_BRAND_ERROR  = HexColor("#FF453A")
_ACCENT_LIGHT = HexColor("#E5E5EA")
_TABLE_HEADER = HexColor("#0A84FF")
_TABLE_ROW_ALT = HexColor("#F2F2F7")
_GREY_TEXT    = HexColor("#636366")

# ---------------------------------------------------------------------------
# Output path
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).resolve().parent
_DOCS_DIR = _BASE_DIR / "docs"
_OUTPUT   = _DOCS_DIR / "QCSystem_Manual.pdf"

# ---------------------------------------------------------------------------
# Page dimensions
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = A4
MARGIN = 20 * mm


def _build_styles() -> dict:
    """Create custom paragraph styles for the manual."""
    ss = getSampleStyleSheet()

    styles = {}

    styles["cover_title"] = ParagraphStyle(
        "cover_title", parent=ss["Title"],
        fontSize=36, leading=44, textColor=_BRAND_DARK,
        alignment=TA_CENTER, spaceAfter=6 * mm,
        fontName="Helvetica-Bold",
    )
    styles["cover_subtitle"] = ParagraphStyle(
        "cover_subtitle", parent=ss["Normal"],
        fontSize=16, leading=22, textColor=_GREY_TEXT,
        alignment=TA_CENTER, spaceAfter=4 * mm,
    )
    styles["cover_version"] = ParagraphStyle(
        "cover_version", parent=ss["Normal"],
        fontSize=11, leading=16, textColor=_GREY_TEXT,
        alignment=TA_CENTER, spaceAfter=2 * mm,
    )
    styles["h1"] = ParagraphStyle(
        "h1", parent=ss["Heading1"],
        fontSize=22, leading=28, textColor=_BRAND_DARK,
        spaceBefore=10 * mm, spaceAfter=4 * mm,
        fontName="Helvetica-Bold",
        borderWidth=0, borderPadding=0,
    )
    styles["h2"] = ParagraphStyle(
        "h2", parent=ss["Heading2"],
        fontSize=16, leading=22, textColor=_BRAND_BLUE,
        spaceBefore=6 * mm, spaceAfter=3 * mm,
        fontName="Helvetica-Bold",
    )
    styles["h3"] = ParagraphStyle(
        "h3", parent=ss["Heading3"],
        fontSize=13, leading=18, textColor=_BRAND_DARK,
        spaceBefore=4 * mm, spaceAfter=2 * mm,
        fontName="Helvetica-Bold",
    )
    styles["body"] = ParagraphStyle(
        "body", parent=ss["Normal"],
        fontSize=10, leading=15, textColor=_BRAND_DARK,
        alignment=TA_JUSTIFY, spaceAfter=2.5 * mm,
    )
    styles["body_small"] = ParagraphStyle(
        "body_small", parent=styles["body"],
        fontSize=9, leading=13,
    )
    styles["note"] = ParagraphStyle(
        "note", parent=styles["body"],
        fontSize=9, leading=13, textColor=_GREY_TEXT,
        leftIndent=8 * mm, borderWidth=1,
        borderColor=_BRAND_BLUE, borderPadding=4,
        backColor=HexColor("#F0F8FF"),
    )
    styles["warning"] = ParagraphStyle(
        "warning", parent=styles["note"],
        borderColor=_BRAND_WARN,
        backColor=HexColor("#FFF8F0"),
    )
    styles["code"] = ParagraphStyle(
        "code", parent=styles["body"],
        fontSize=9, leading=12,
        fontName="Courier",
        leftIndent=6 * mm,
        backColor=HexColor("#F5F5F7"),
        borderWidth=0.5, borderColor=_ACCENT_LIGHT,
        borderPadding=4, spaceAfter=3 * mm,
    )
    styles["toc"] = ParagraphStyle(
        "toc", parent=styles["body"],
        fontSize=11, leading=18, spaceAfter=1 * mm,
    )
    styles["footer"] = ParagraphStyle(
        "footer", parent=ss["Normal"],
        fontSize=8, textColor=_GREY_TEXT, alignment=TA_CENTER,
    )
    styles["table_header"] = ParagraphStyle(
        "table_header", parent=styles["body"],
        fontSize=9, leading=12, textColor=white,
        fontName="Helvetica-Bold",
    )
    styles["table_cell"] = ParagraphStyle(
        "table_cell", parent=styles["body"],
        fontSize=9, leading=12,
    )
    return styles


def _table(headers: list[str], rows: list[list[str]], styles: dict,
           col_widths: list[float] | None = None) -> Table:
    """Build a styled table with alternating row colours."""
    hdr_cells = [Paragraph(h, styles["table_header"]) for h in headers]
    body_cells = [
        [Paragraph(str(c), styles["table_cell"]) for c in row]
        for row in rows
    ]
    data = [hdr_cells] + body_cells

    avail = PAGE_W - 2 * MARGIN
    if col_widths is None:
        n = len(headers)
        col_widths = [avail / n] * n

    t = Table(data, colWidths=col_widths, repeatRows=1)
    style_cmds = [
        ("BACKGROUND",   (0, 0), (-1, 0), _TABLE_HEADER),
        ("TEXTCOLOR",    (0, 0), (-1, 0), white),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING",(0, 0), (-1, 0), 6),
        ("TOPPADDING",   (0, 0), (-1, 0), 6),
        ("GRID",         (0, 0), (-1, -1), 0.4, _ACCENT_LIGHT),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",   (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 1), (-1, -1), 4),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), _TABLE_ROW_ALT))
    t.setStyle(TableStyle(style_cmds))
    return t


def _hr() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.5, color=_ACCENT_LIGHT,
                      spaceAfter=3 * mm, spaceBefore=3 * mm)


def _bullet_list(items: list[str], styles: dict) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(item, styles["body"]), bulletColor=_BRAND_BLUE)
         for item in items],
        bulletType="bullet", bulletFontSize=8, leftIndent=10 * mm,
        spaceAfter=3 * mm,
    )


# ===========================================================================
# Content sections
# ===========================================================================

def _cover(S: dict) -> list:
    """Title page."""
    return [
        Spacer(1, 50 * mm),
        Paragraph("QC System", S["cover_title"]),
        Paragraph("Multi-Camera Industrial Quality Control", S["cover_subtitle"]),
        Spacer(1, 10 * mm),
        _hr(),
        Spacer(1, 6 * mm),
        Paragraph("User &amp; Installation Manual", S["cover_subtitle"]),
        Spacer(1, 8 * mm),
        Paragraph(f"Version 1.0 &mdash; {datetime.now().strftime('%B %Y')}", S["cover_version"]),
        Paragraph("Confidential &mdash; For Authorised Personnel Only", S["cover_version"]),
        Spacer(1, 30 * mm),
        Paragraph(
            "This document covers system requirements, installation, configuration, "
            "operation, PLC integration, user management, and troubleshooting for "
            "the QC System multi-camera inspection platform.",
            S["body"],
        ),
        PageBreak(),
    ]


def _toc(S: dict) -> list:
    """Table of contents."""
    entries = [
        "1. System Overview",
        "2. System Requirements",
        "3. Installation",
        "4. First Launch &amp; Initial Setup",
        "5. Application Settings Reference",
        "    5.1 Inspection Tab",
        "    5.2 Cameras Tab",
        "    5.3 Model Tab",
        "    5.4 System Tab",
        "    5.5 PLC Tab (Siemens S7)",
        "    5.6 PROFINET Tab",
        "    5.7 Beckhoff Tab",
        "6. Authentication &amp; User Management",
        "7. Operating the System",
        "8. PLC Integration Guide",
        "9. Data &amp; Audit Trail",
        "10. Troubleshooting",
        "11. Appendix: settings.json Reference",
    ]
    elems = [
        Paragraph("Table of Contents", S["h1"]),
        _hr(),
    ]
    for e in entries:
        elems.append(Paragraph(e, S["toc"]))
    elems.append(PageBreak())
    return elems


def _section_overview(S: dict) -> list:
    """Section 1 — System Overview."""
    return [
        Paragraph("1. System Overview", S["h1"]),
        Paragraph(
            "The QC System is a real-time, multi-camera visual inspection platform "
            "designed for high-speed production lines. It uses YOLOv8 object detection "
            "running on ONNX Runtime to count items on trays and flag missing-item defects "
            "instantly.",
            S["body"],
        ),
        Paragraph("Key Capabilities", S["h3"]),
        _bullet_list([
            "Up to 6 simultaneous USB / RTSP camera feeds at full frame rate",
            "YOLOv8 ONNX inference with per-camera dedicated threads",
            "Real-time OK / MISSING / NO TRAY classification per capture",
            "Automatic defect image archiving with annotated bounding boxes",
            "Batch-based workflow with PDF report generation",
            "Three PLC integration modes: Siemens S7 (snap7), PROFINET IO Device, Beckhoff TwinCAT ADS",
            "Hot-reloadable fieldbus settings (no restart required)",
            "Active Directory / LDAP authentication with offline cache fallback",
            "Local user management with role-based access control",
            "Comprehensive audit trail (JSONL daily rotation)",
            "Single-EXE deployment via PyInstaller (Windows 10/11)",
        ], S),
        Paragraph("Architecture", S["h3"]),
        Paragraph(
            "The application follows a layered architecture with clear separation of "
            "concerns: <b>core/</b> (detector, counter, tracker), <b>services/</b> "
            "(camera, inference, storage, defect, report, PLC, PROFINET, Beckhoff, audit), "
            "<b>auth/</b> (LDAP, user cache, permissions, decorators), and <b>ui/</b> "
            "(PySide6 widgets). All inter-layer communication uses Qt signals so the UI "
            "thread is never blocked by I/O or inference.",
            S["body"],
        ),
        PageBreak(),
    ]


def _section_requirements(S: dict) -> list:
    """Section 2 — System Requirements."""
    return [
        Paragraph("2. System Requirements", S["h1"]),
        Paragraph("Hardware", S["h2"]),
        _table(
            ["Component", "Minimum", "Recommended"],
            [
                ["CPU", "Intel Core i5 (4 cores)", "Intel Core i7 / Xeon (8+ cores)"],
                ["RAM", "8 GB", "16 GB or more"],
                ["GPU", "Not required (CPU inference)", "NVIDIA GPU with CUDA for TensorRT EP"],
                ["Storage", "256 GB SSD", "512 GB SSD (defect images accumulate)"],
                ["Cameras", "USB 2.0 webcam", "USB 3.0 industrial cameras or GigE via RTSP"],
                ["Network", "Ethernet (for PLC)", "Dedicated inspection VLAN"],
                ["Display", "1280 x 720", "1920 x 1080 or higher"],
            ],
            S,
            col_widths=[40 * mm, 55 * mm, 60 * mm],
        ),
        Spacer(1, 4 * mm),
        Paragraph("Software", S["h2"]),
        _table(
            ["Software", "Version", "Notes"],
            [
                ["Windows", "10 / 11 (64-bit)", "LTSC editions supported"],
                ["Python", "3.11+", "Only for development; not needed for EXE deployment"],
                ["ONNX Runtime", "1.16+", "Bundled in the installer"],
                ["PySide6", "6.6+", "Bundled in the installer"],
                ["Npcap", "1.7+", "Required ONLY for PROFINET IO Device mode"],
                ["python-snap7", "1.3+", "Required ONLY for Siemens S7 PLC mode"],
                ["pyads", "3.3+", "Required ONLY for Beckhoff TwinCAT ADS mode"],
                ["scapy", "2.5+", "Required ONLY for PROFINET IO Device mode"],
            ],
            S,
            col_widths=[35 * mm, 30 * mm, 90 * mm],
        ),
        PageBreak(),
    ]


def _section_installation(S: dict) -> list:
    """Section 3 — Installation."""
    return [
        Paragraph("3. Installation", S["h1"]),
        Paragraph("Pre-Built Executable (Recommended)", S["h2"]),
        Paragraph(
            "The QC System is distributed as a self-contained folder produced by PyInstaller. "
            "No Python installation is required on the target machine.",
            S["body"],
        ),
        _bullet_list([
            "Copy the <b>QCSystem/</b> folder to the target PC (e.g. <font face='Courier'>C:\\QCSystem\\</font>).",
            "Ensure the <b>models/</b> subdirectory contains your trained ONNX model file.",
            "Double-click <b>QCSystem.exe</b> to launch the application.",
            "On first run, a default <font face='Courier'>settings.json</font> is created automatically.",
        ], S),
        Paragraph("Development Setup", S["h2"]),
        Paragraph(
            "For development and customisation, clone the repository and install dependencies:",
            S["body"],
        ),
        Paragraph(
            "git clone https://github.com/AlexNELE/qc_system.git<br/>"
            "cd qc_system<br/>"
            "pip install PySide6 opencv-python onnxruntime numpy ldap3 reportlab<br/>"
            "python main.py",
            S["code"],
        ),
        Paragraph("Optional PLC dependencies (install only what you need):", S["body"]),
        Paragraph(
            "pip install python-snap7    # Siemens S7 mode<br/>"
            "pip install pyads           # Beckhoff TwinCAT ADS mode<br/>"
            "pip install scapy           # PROFINET IO Device mode (+ install Npcap)",
            S["code"],
        ),
        Paragraph("Building the Installer", S["h2"]),
        Paragraph(
            "To create a distributable build:",
            S["body"],
        ),
        Paragraph(
            "pip install pyinstaller<br/>"
            "pyinstaller QCSystem.spec",
            S["code"],
        ),
        Paragraph(
            "The output is placed in <font face='Courier'>dist/QCSystem/</font>. "
            "The <font face='Courier'>docs/QCSystem_Manual.pdf</font> file (this document) "
            "is automatically included in the build.",
            S["body"],
        ),
        Paragraph("Directory Structure After Installation", S["h3"]),
        Paragraph(
            "QCSystem/<br/>"
            "&nbsp;&nbsp;QCSystem.exe<br/>"
            "&nbsp;&nbsp;settings.json<br/>"
            "&nbsp;&nbsp;models/<br/>"
            "&nbsp;&nbsp;&nbsp;&nbsp;yolov8_model.onnx<br/>"
            "&nbsp;&nbsp;docs/<br/>"
            "&nbsp;&nbsp;&nbsp;&nbsp;QCSystem_Manual.pdf<br/>"
            "&nbsp;&nbsp;defects/<br/>"
            "&nbsp;&nbsp;captures/<br/>"
            "&nbsp;&nbsp;reports/<br/>"
            "&nbsp;&nbsp;audit_logs/<br/>"
            "&nbsp;&nbsp;logs/<br/>"
            "&nbsp;&nbsp;qc_results.db<br/>"
            "&nbsp;&nbsp;user_cache.db",
            S["code"],
        ),
        PageBreak(),
    ]


def _section_first_launch(S: dict) -> list:
    """Section 4 — First Launch."""
    return [
        Paragraph("4. First Launch &amp; Initial Setup", S["h1"]),
        Paragraph(
            "When the application starts for the first time, it performs the following steps:",
            S["body"],
        ),
        _bullet_list([
            "<b>settings.json</b> is created with default values if not present.",
            "The SQLite databases (<font face='Courier'>qc_results.db</font>, <font face='Courier'>user_cache.db</font>) are initialised.",
            "The <font face='Courier'>audit_logs/</font>, <font face='Courier'>defects/</font>, <font face='Courier'>captures/</font>, and <font face='Courier'>reports/</font> directories are created.",
            "If Active Directory is enabled (default), the Login screen is shown.",
            "If Active Directory is disabled, the application starts immediately with an Administrator session.",
        ], S),
        Paragraph("Quick Start for Standalone (No AD) Deployment", S["h2"]),
        Paragraph(
            "To skip Active Directory and start immediately as Administrator:",
            S["body"],
        ),
        _bullet_list([
            'Open <font face="Courier">settings.json</font> in a text editor.',
            'Set <font face="Courier">"active_directory_enabled": false</font> in the <font face="Courier">"auth"</font> section.',
            "Save the file and restart the application.",
            "The application will start directly with full Administrator access.",
        ], S),
        Paragraph(
            "<b>Note:</b> You can still create local user accounts via the User Management dialog "
            "and re-enable the Login button by setting <font face='Courier'>\"login_required\": true</font>.",
            S["note"],
        ),
        PageBreak(),
    ]


def _section_settings(S: dict) -> list:
    """Section 5 — Application Settings Reference."""
    avail = PAGE_W - 2 * MARGIN
    c1, c2, c3 = 38 * mm, 25 * mm, avail - 63 * mm

    elems = [
        Paragraph("5. Application Settings Reference", S["h1"]),
        Paragraph(
            "All settings are accessible via <b>Application Settings</b> in the toolbar "
            "(requires Supervisor or Administrator role). Changes are saved to "
            "<font face='Courier'>settings.json</font> and applied immediately.",
            S["body"],
        ),
    ]

    # --- 5.1 Inspection ---
    elems += [
        Paragraph("5.1 Inspection Tab", S["h2"]),
        Paragraph(
            "Controls the object detection and counting parameters.",
            S["body"],
        ),
        _table(
            ["Parameter", "Default", "Description"],
            [
                ["Expected object count", "160",
                 "Number of items expected per tray. A capture with fewer detections is flagged as MISSING."],
                ["Confidence threshold", "0.50",
                 "Minimum detection confidence (0.01 \u2013 1.00). Lower values detect more objects but increase false positives."],
                ["IoU threshold (NMS)", "0.45",
                 "Intersection-over-Union threshold for Non-Maximum Suppression. Controls overlap filtering between detections."],
                ["Target class ID", "0",
                 "ONNX model class index to count. Set to the class ID of your target object in the trained model."],
                ["Save annotated images", "On",
                 "When enabled, saves bounding-box-annotated images for MISSING captures to the defects/ directory."],
            ],
            S,
            col_widths=[c1, c2, c3],
        ),
    ]

    # --- 5.2 Cameras ---
    elems += [
        Paragraph("5.2 Cameras Tab", S["h2"]),
        Paragraph(
            "Manages the list of camera sources. Each entry is either an integer index "
            "(for USB cameras, e.g. <font face='Courier'>0</font>, <font face='Courier'>1</font>) "
            "or an RTSP URL (e.g. <font face='Courier'>rtsp://192.168.1.100/stream</font>).",
            S["body"],
        ),
        _table(
            ["Action", "Description"],
            [
                ["Add", "Enter a camera index or RTSP URL and click Add to append it to the list."],
                ["Remove", "Select a camera in the list and click Remove to delete it."],
            ],
            S,
            col_widths=[30 * mm, avail - 30 * mm],
        ),
        Paragraph(
            "<b>Note:</b> Camera changes trigger a live reload of the camera grid. "
            "Active batches should be ended before modifying cameras.",
            S["note"],
        ),
    ]

    # --- 5.3 Model ---
    elems += [
        Paragraph("5.3 Model Tab", S["h2"]),
        _table(
            ["Parameter", "Default", "Description"],
            [
                ["ONNX model file path", "models/yolov8_model.onnx",
                 "Path to the YOLOv8 ONNX model file. Use Browse to select a different model. "
                 "The model is reloaded live when changed."],
            ],
            S,
            col_widths=[c1, c2, c3],
        ),
    ]

    # --- 5.4 System ---
    elems += [
        Paragraph("5.4 System Tab", S["h2"]),
        _table(
            ["Parameter", "Default", "Description"],
            [
                ["Log level", "DEBUG",
                 "Controls verbosity of the application log. Options: DEBUG, INFO, WARNING, ERROR. "
                 "Production systems should use INFO or WARNING."],
                ["Active Directory authentication", "On",
                 "Enable or disable LDAP/AD authentication. When disabled, the application starts "
                 "immediately without a login prompt."],
                ["Login required", "On",
                 "When unchecked, the app starts with an automatic Operator session. The Login button "
                 "remains available for elevated access."],
                ["Default role (AD disabled)", "ADMIN",
                 "Role assigned to the automatic session when AD is disabled. Options: OPERATOR, SUPERVISOR, ADMIN."],
            ],
            S,
            col_widths=[c1, c2, c3],
        ),
    ]

    # --- 5.5 PLC ---
    elems += [
        PageBreak(),
        Paragraph("5.5 PLC Tab (Siemens S7)", S["h2"]),
        Paragraph(
            "Configures the Siemens S7-1500 / S7-1200 communication interface via snap7 (ISO TCP, port 102).",
            S["body"],
        ),
        _table(
            ["Parameter", "Default", "Description"],
            [
                ["Enable S7 PLC interface", "Off",
                 "Activate the PLCService background thread. Requires python-snap7 to be installed."],
                ["PLC IP address", "192.168.0.1",
                 "IPv4 address of the Siemens PLC Ethernet interface."],
                ["Rack", "0",
                 "PLC rack number. Always 0 for S7-1500 and S7-1200."],
                ["Slot", "1",
                 "PLC CPU slot number. Always 1 for S7-1500; 0 for S7-1200."],
                ["Data Block number", "100",
                 "The non-optimised DB number (e.g. 100 for DB100) that holds the 16-byte exchange struct."],
                ["Poll interval (ms)", "50",
                 "Read/write cycle period in milliseconds. Lower values increase responsiveness but CPU load."],
                ["Reconnect delay (s)", "3.0",
                 "Initial wait before retrying after a connection failure. Doubles on each retry."],
                ["Max reconnect delay (s)", "30.0",
                 "Upper cap for the exponential back-off reconnect timer."],
            ],
            S,
            col_widths=[c1, c2, c3],
        ),
        Paragraph(
            "<b>TIA Portal setup:</b> Create a non-optimised Global DB with the 16-byte layout. "
            "Enable PUT/GET communication under CPU Protection &amp; Security settings.",
            S["note"],
        ),
    ]

    # --- 5.6 PROFINET ---
    elems += [
        Paragraph("5.6 PROFINET Tab", S["h2"]),
        Paragraph(
            "Configures the PROFINET IO Device interface (Mode B). The QC System acts as a "
            "PROFINET IO Device that is controlled by a PROFINET IO Controller (e.g. S7-1500).",
            S["body"],
        ),
        _table(
            ["Parameter", "Default", "Description"],
            [
                ["Enable PROFINET", "Off",
                 "Activate the ProfinetService. Requires scapy and Npcap."],
                ["Network interface", "Ethernet",
                 "OS network interface name (e.g. \"Ethernet\", \"eth0\"). Must match the physical adapter connected to the PLC network."],
                ["Station name", "qc-inspection-sys",
                 "PROFINET station name (DNS label format). Must match the name configured in TIA Portal."],
                ["MAC address", "(empty)",
                 "MAC address of the selected interface in AA:BB:CC:DD:EE:FF format."],
                ["IP address", "192.168.0.2",
                 "Device IP address. Must match the TIA Portal device configuration."],
                ["Subnet mask", "255.255.255.0",
                 "Subnet mask for the PROFINET network."],
                ["Gateway", "192.168.0.1",
                 "Default gateway (typically the PLC IP for point-to-point links)."],
                ["Cycle time (ms)", "4",
                 "RT cyclic frame send interval. Must match the send clock configured in TIA Portal."],
                ["Watchdog (ms)", "200",
                 "Maximum time without an output frame before the Application Relationship is torn down."],
            ],
            S,
            col_widths=[c1, c2, c3],
        ),
    ]

    # --- 5.7 Beckhoff ---
    elems += [
        PageBreak(),
        Paragraph("5.7 Beckhoff Tab", S["h2"]),
        Paragraph(
            "Configures the Beckhoff TwinCAT ADS communication interface. Uses the pyads "
            "library to exchange data with a TwinCAT 3 PLC runtime via ADS/TCP (port 48898).",
            S["body"],
        ),
        _table(
            ["Parameter", "Default", "Description"],
            [
                ["Enable Beckhoff ADS", "Off",
                 "Activate the BeckhoffService background thread. Requires pyads to be installed."],
                ["AMS Net ID", "5.80.201.232.1.1",
                 "AMS Net ID of the TwinCAT runtime (found in System Manager \u2192 Routes)."],
                ["AMS port", "851",
                 "ADS port number. 851 = TwinCAT 3 PLC Runtime 1; 852 = Runtime 2."],
                ["PLC symbol name", "GVL.stQC",
                 "Fully qualified TwinCAT symbol path for the 16-byte exchange struct."],
                ["Poll interval (ms)", "50",
                 "Read/write cycle period in milliseconds."],
                ["Reconnect delay (s)", "3.0",
                 "Initial wait before retrying after a connection failure."],
                ["Max reconnect delay (s)", "30.0",
                 "Upper cap for the exponential back-off reconnect timer."],
            ],
            S,
            col_widths=[c1, c2, c3],
        ),
        Paragraph(
            "<b>TwinCAT setup:</b> Create a GVL with the ST_QC_Interface struct type. "
            "Add an ADS route from the PC to the TwinCAT runtime. See Section 8 for the "
            "complete struct definition.",
            S["note"],
        ),
        Paragraph("Fieldbus Priority", S["h3"]),
        Paragraph(
            "Only one fieldbus interface can be active at a time. If multiple interfaces are "
            "enabled, the following priority applies: <b>PROFINET</b> &gt; <b>Beckhoff ADS</b> &gt; "
            "<b>Siemens S7</b>. All fieldbus settings are hot-reloadable \u2014 changes take effect "
            "immediately without restarting the application.",
            S["body"],
        ),
        PageBreak(),
    ]

    return elems


def _section_auth(S: dict) -> list:
    """Section 6 — Authentication & User Management."""
    avail = PAGE_W - 2 * MARGIN
    return [
        Paragraph("6. Authentication &amp; User Management", S["h1"]),
        Paragraph("Role-Based Access Control", S["h2"]),
        Paragraph(
            "The QC System uses a three-tier role hierarchy. Each role inherits all permissions "
            "of the roles below it.",
            S["body"],
        ),
        _table(
            ["Role", "Level", "Permissions"],
            [
                ["Operator", "10",
                 "View live feed, start/end batches, press Capture All, view/export reports."],
                ["Supervisor", "20",
                 "All Operator permissions plus: change application settings, manage users."],
                ["Administrator", "30",
                 "All Supervisor permissions (same permission set; reserved for future extensions)."],
            ],
            S,
            col_widths=[30 * mm, 18 * mm, avail - 48 * mm],
        ),
        Paragraph("Authentication Modes", S["h2"]),
        Paragraph("<b>Mode 1: Active Directory (LDAP)</b>", S["h3"]),
        Paragraph(
            "When <font face='Courier'>active_directory_enabled</font> is <font face='Courier'>true</font>, "
            "the application shows a Login dialog on startup. Users authenticate against Active Directory "
            "domain controllers configured in <font face='Courier'>ldap_servers</font>. AD group membership "
            "is mapped to roles via <font face='Courier'>ldap_group_role_map</font>.",
            S["body"],
        ),
        Paragraph(
            "If the AD server is unreachable, the system falls back to the local offline cache "
            "(credentials are cached after each successful LDAP login).",
            S["body"],
        ),
        Paragraph("<b>Mode 2: Local Accounts Only</b>", S["h3"]),
        Paragraph(
            "When <font face='Courier'>active_directory_enabled</font> is <font face='Courier'>false</font>, "
            "no LDAP connection is attempted. The application starts immediately with a local session "
            "using the role specified in <font face='Courier'>no_auth_default_role</font>. "
            "Local accounts can still be created via the User Management dialog.",
            S["body"],
        ),
        Paragraph("User Management", S["h2"]),
        Paragraph(
            "Accessible via the toolbar (Administrator/Supervisor only). Allows creating, editing, "
            "and deleting local user accounts. Each account has: username, display name, role, "
            "email, and a force-password-change flag.",
            S["body"],
        ),
        Paragraph(
            "Local accounts are stored in <font face='Courier'>user_cache.db</font> with PBKDF2 "
            "password hashing (bcrypt is used if installed). The database is separate from the QC "
            "results database.",
            S["body"],
        ),
        PageBreak(),
    ]


def _section_operation(S: dict) -> list:
    """Section 7 — Operating the System."""
    return [
        Paragraph("7. Operating the System", S["h1"]),
        Paragraph("Batch Workflow", S["h2"]),
        Paragraph(
            "The QC System operates in a batch-based workflow. Each batch represents one "
            "production run and is identified by a unique Batch ID.",
            S["body"],
        ),
        _bullet_list([
            "<b>Step 1 \u2014 Enter Batch ID:</b> Type a unique identifier in the Batch ID field (e.g. <font face='Courier'>BATCH-2026-0315-001</font>).",
            "<b>Step 2 \u2014 Start Batch:</b> Click <b>Batch Start</b>. All cameras begin streaming. The Batch ID field is locked.",
            "<b>Step 3 \u2014 Capture:</b> Click <b>Capture All</b> (or wait for a PLC trigger) to inspect the current tray. Each camera captures simultaneously.",
            "<b>Step 4 \u2014 Review:</b> Each camera panel shows OK (green), MISSING (red), or NO TRAY (grey). Running statistics are displayed per camera.",
            "<b>Step 5 \u2014 End Batch:</b> Click <b>Batch End</b>. Cameras stop. A PDF report is generated automatically in the <font face='Courier'>reports/</font> directory.",
        ], S),
        Paragraph("Capture Results", S["h2"]),
        _table(
            ["Status", "Colour", "Meaning"],
            [
                ["OK", "Green", "Detected count equals or exceeds the expected count. Tray passes inspection."],
                ["MISSING", "Red", "Detected count is below the expected count. Items are missing. Defect images are saved."],
                ["NO TRAY", "Grey", "Zero detections. No tray present at this position. Excluded from all statistics."],
            ],
            S,
            col_widths=[25 * mm, 20 * mm, PAGE_W - 2 * MARGIN - 45 * mm],
        ),
        Paragraph("PLC-Triggered Capture", S["h3"]),
        Paragraph(
            "When a PLC interface is active, the PLC can trigger captures by setting the trigger "
            "bit (bit 0.0) in the exchange data block. The rising edge is detected by the QC System "
            "and all cameras capture simultaneously \u2014 identical to pressing Capture All.",
            S["body"],
        ),
        Paragraph(
            "The inhibit bit (bit 0.7) can be used by the PLC to suppress captures during E-Stop "
            "or safety interlock conditions.",
            S["body"],
        ),
        PageBreak(),
    ]


def _section_plc(S: dict) -> list:
    """Section 8 — PLC Integration Guide."""
    avail = PAGE_W - 2 * MARGIN
    return [
        Paragraph("8. PLC Integration Guide", S["h1"]),
        Paragraph("Data Block Layout (16 bytes)", S["h2"]),
        Paragraph(
            "All three fieldbus interfaces (S7, PROFINET, Beckhoff) use an identical 16-byte "
            "data structure. This ensures drop-in compatibility when switching between interfaces.",
            S["body"],
        ),
        _table(
            ["Offset", "Type", "Name", "Direction", "Description"],
            [
                ["Byte 0, Bit 0", "BOOL", "trigger", "PLC \u2192 PC", "Rising edge triggers capture"],
                ["Byte 0, Bit 1", "BOOL", "batch_active", "PC \u2192 PLC", "Batch is currently running"],
                ["Byte 0, Bit 2", "BOOL", "result_ok", "PC \u2192 PLC", "Last capture was OK"],
                ["Byte 0, Bit 3", "BOOL", "result_defect", "PC \u2192 PLC", "Last capture was MISSING"],
                ["Byte 0, Bit 4", "BOOL", "heartbeat", "PC \u2192 PLC", "Toggles every 1 s (alive)"],
                ["Byte 0, Bit 5", "BOOL", "system_ready", "PC \u2192 PLC", "All cameras initialised"],
                ["Byte 0, Bit 6", "BOOL", "ack_trigger", "PC \u2192 PLC", "High during capture processing"],
                ["Byte 0, Bit 7", "BOOL", "inhibit", "PLC \u2192 PC", "Suppress capture (E-Stop)"],
                ["Byte 1", "BYTE", "(padding)", "\u2014", "Word-alignment pad"],
                ["Bytes 2\u20133", "INT", "detected_count", "PC \u2192 PLC", "Items detected in last capture"],
                ["Bytes 4\u20135", "INT", "expected_count", "PC \u2192 PLC", "Configured expected count"],
                ["Bytes 6\u20137", "INT", "camera_id", "PC \u2192 PLC", "Camera that produced result"],
                ["Bytes 8\u20139", "INT", "defect_count", "PC \u2192 PLC", "Running MISSING tally"],
                ["Bytes 10\u201311", "INT", "ok_count", "PC \u2192 PLC", "Running OK tally"],
                ["Bytes 12\u201315", "DINT", "batch_id_hash", "PC \u2192 PLC", "CRC-32 of batch ID string"],
            ],
            S,
            col_widths=[22 * mm, 14 * mm, 28 * mm, 22 * mm, avail - 86 * mm],
        ),
        Spacer(1, 4 * mm),
        Paragraph("Siemens TIA Portal Setup", S["h2"]),
        _bullet_list([
            "Create a non-optimised Global Data Block (e.g. DB100) with the layout above.",
            "Uncheck <b>Optimized block access</b> in the DB properties so byte offsets are fixed.",
            "Enable <b>PUT/GET communication</b>: CPU Properties \u2192 Protection &amp; Security \u2192 Connection mechanisms \u2192 Permit access with PUT/GET.",
            "Ensure the PC IP address is reachable from the PLC (same subnet or routed).",
            "Set PLC watchdog to 3000 ms on the heartbeat bit (Bit 0.4) to detect PC failure.",
        ], S),
        Paragraph("Beckhoff TwinCAT Setup", S["h2"]),
        Paragraph(
            "Create the following struct type in your TwinCAT project:",
            S["body"],
        ),
        Paragraph(
            "TYPE ST_QC_Interface :<br/>"
            "STRUCT<br/>"
            "&nbsp;&nbsp;bFlags         : BYTE;<br/>"
            "&nbsp;&nbsp;bPadding       : BYTE;<br/>"
            "&nbsp;&nbsp;nDetectedCount : INT;<br/>"
            "&nbsp;&nbsp;nExpectedCount : INT;<br/>"
            "&nbsp;&nbsp;nCameraId      : INT;<br/>"
            "&nbsp;&nbsp;nDefectCount   : INT;<br/>"
            "&nbsp;&nbsp;nOkCount       : INT;<br/>"
            "&nbsp;&nbsp;nBatchIdHash   : DINT;<br/>"
            "END_STRUCT<br/>"
            "END_TYPE",
            S["code"],
        ),
        _bullet_list([
            "Declare an instance in a GVL: <font face='Courier'>stQC : ST_QC_Interface;</font>",
            "Add an ADS route from the PC to the TwinCAT runtime (System Manager \u2192 Routes).",
            "Note the AMS Net ID and ADS port (851 for Runtime 1).",
        ], S),
        PageBreak(),
    ]


def _section_data(S: dict) -> list:
    """Section 9 — Data & Audit Trail."""
    avail = PAGE_W - 2 * MARGIN
    return [
        Paragraph("9. Data &amp; Audit Trail", S["h1"]),
        Paragraph("Database Files", S["h2"]),
        _table(
            ["File", "Purpose"],
            [
                ["qc_results.db", "SQLite database storing all inspection results, batch metadata, and capture records."],
                ["user_cache.db", "SQLite database for local user accounts, LDAP offline cache, and password hashes."],
            ],
            S,
            col_widths=[35 * mm, avail - 35 * mm],
        ),
        Paragraph("Output Directories", S["h2"]),
        _table(
            ["Directory", "Contents"],
            [
                ["defects/", "Annotated images of MISSING captures, organised by batch ID and timestamp."],
                ["captures/", "All captured frames (OK and MISSING), organised by batch ID."],
                ["reports/", "Generated PDF batch reports with summary statistics and per-camera results."],
                ["logs/", "Application log files."],
                ["audit_logs/", "JSONL audit trail files (one per day)."],
            ],
            S,
            col_widths=[30 * mm, avail - 30 * mm],
        ),
        Paragraph("Audit Trail", S["h2"]),
        Paragraph(
            "The QC System maintains a tamper-evident audit trail in "
            "<font face='Courier'>audit_logs/</font>. Each file is named "
            "<font face='Courier'>audit_YYYY-MM-DD.jsonl</font> and contains one JSON object per line.",
            S["body"],
        ),
        Paragraph("Tracked Events", S["h3"]),
        _table(
            ["Event Type", "Trigger"],
            [
                ["APP_START", "Application launched"],
                ["APP_SHUTDOWN", "Application closed"],
                ["LOGIN", "User successfully authenticated"],
                ["LOGOUT", "User logged out"],
                ["LOGIN_FAILED", "Failed authentication attempt"],
                ["BATCH_START", "Batch started (includes batch ID)"],
                ["BATCH_END", "Batch ended (includes OK/defect counts)"],
                ["CAPTURE", "Single camera capture (includes camera ID, status, counts)"],
                ["SETTINGS_CHANGED", "Application settings modified"],
                ["PLC_CONNECTED", "Fieldbus interface connected"],
                ["PLC_DISCONNECTED", "Fieldbus interface disconnected"],
                ["USER_CREATED", "New local user account created"],
                ["USER_DELETED", "Local user account deleted"],
                ["USER_ROLE_CHANGED", "User role modified"],
            ],
            S,
            col_widths=[35 * mm, avail - 35 * mm],
        ),
        Paragraph("Example Audit Entry", S["h3"]),
        Paragraph(
            '{"timestamp": "2026-03-15T14:23:07.412345+00:00",<br/>'
            '&nbsp;"event_type": "CAPTURE",<br/>'
            '&nbsp;"user": "jdoe",<br/>'
            '&nbsp;"role": "OPERATOR",<br/>'
            '&nbsp;"camera_id": 2,<br/>'
            '&nbsp;"status": "OK",<br/>'
            '&nbsp;"detected": 160,<br/>'
            '&nbsp;"expected": 160}',
            S["code"],
        ),
        PageBreak(),
    ]


def _section_troubleshooting(S: dict) -> list:
    """Section 10 — Troubleshooting."""
    avail = PAGE_W - 2 * MARGIN
    return [
        Paragraph("10. Troubleshooting", S["h1"]),
        _table(
            ["Symptom", "Cause", "Solution"],
            [
                ["Camera feed is black",
                 "Camera not connected or index incorrect",
                 "Check USB connection. Verify camera index in Settings \u2192 Cameras."],
                ["ONNX model fails to load",
                 "Model file missing or incompatible",
                 "Ensure model exists at the configured path. Re-export with: yolo export model=best.pt format=onnx"],
                ["PLC connection fails",
                 "IP unreachable or PUT/GET not enabled",
                 "Ping PLC from PC. Enable PUT/GET in TIA Portal. Check firewall rules."],
                ["PROFINET AR not established",
                 "Interface or MAC incorrect",
                 "Verify interface name matches OS adapter. Ensure Npcap is installed. Check station name matches TIA Portal."],
                ["Beckhoff ADS timeout",
                 "AMS Net ID incorrect or no route",
                 "Verify AMS Net ID in TwinCAT System Manager. Add ADS route from PC to PLC."],
                ["Login always fails",
                 "LDAP server unreachable, no cached credentials",
                 "Check network. For first-time setup without AD, set active_directory_enabled to false."],
                ["\"Permission denied\" on actions",
                 "Logged-in role lacks permission",
                 "Log in as Supervisor or Administrator. Check role mappings in ldap_group_role_map."],
                ["Audit log not created",
                 "Write permission denied on audit_logs/",
                 "Ensure the application has write access to its installation directory."],
            ],
            S,
            col_widths=[35 * mm, 38 * mm, avail - 73 * mm],
        ),
        Paragraph(
            "<b>For additional support</b>, check the application logs in the <font face='Courier'>logs/</font> "
            "directory. Set <font face='Courier'>log_level</font> to <font face='Courier'>DEBUG</font> "
            "for maximum detail.",
            S["note"],
        ),
        PageBreak(),
    ]


def _section_appendix(S: dict) -> list:
    """Section 11 — Appendix: Full settings.json reference."""
    avail = PAGE_W - 2 * MARGIN
    c1, c2, c3, c4 = 35 * mm, 30 * mm, 22 * mm, avail - 87 * mm

    return [
        Paragraph("11. Appendix: settings.json Reference", S["h1"]),
        Paragraph(
            "Complete reference of all keys in <font face='Courier'>settings.json</font>. "
            "Missing keys are filled with defaults automatically.",
            S["body"],
        ),
        Paragraph("Top-Level Keys", S["h2"]),
        _table(
            ["Key", "Type", "Default", "Description"],
            [
                ["cameras", "list", "[0,1,2,3]", "Camera source indices or RTSP URLs"],
                ["expected_count", "int", "160", "Expected items per tray"],
                ["model_path", "string", "models/yolov8_model.onnx", "Path to ONNX model"],
                ["conf_threshold", "float", "0.5", "Detection confidence threshold"],
                ["iou_threshold", "float", "0.45", "NMS IoU threshold"],
                ["target_class_id", "int", "0", "ONNX class index to count"],
                ["camera_reconnect_delay", "float", "2.0", "Camera reconnect delay (s)"],
                ["camera_reconnect_max", "float", "30.0", "Max camera reconnect delay (s)"],
                ["save_annotated_images", "bool", "true", "Save annotated defect images"],
                ["log_level", "string", "DEBUG", "Log level (DEBUG/INFO/WARNING/ERROR)"],
            ],
            S,
            col_widths=[c1, c2, c3, c4],
        ),
        Paragraph("plc.*  Keys", S["h2"]),
        _table(
            ["Key", "Type", "Default", "Description"],
            [
                ["enabled", "bool", "false", "Enable S7 PLC interface"],
                ["ip", "string", "192.168.0.1", "PLC IP address"],
                ["rack", "int", "0", "PLC rack number"],
                ["slot", "int", "1", "PLC slot number"],
                ["db_number", "int", "100", "Data Block number"],
                ["poll_interval_ms", "int", "50", "Poll cycle (ms)"],
                ["reconnect_delay", "float", "3.0", "Initial reconnect delay (s)"],
                ["reconnect_max", "float", "30.0", "Max reconnect delay (s)"],
            ],
            S,
            col_widths=[c1, c2, c3, c4],
        ),
        Paragraph("profinet.*  Keys", S["h2"]),
        _table(
            ["Key", "Type", "Default", "Description"],
            [
                ["enabled", "bool", "false", "Enable PROFINET IO Device"],
                ["interface", "string", '""', "OS network interface name"],
                ["station_name", "string", "qc-inspection-sys", "PROFINET station name"],
                ["mac_address", "string", '""', "Interface MAC (AA:BB:CC:DD:EE:FF)"],
                ["ip_address", "string", "192.168.0.2", "Device IP address"],
                ["subnet_mask", "string", "255.255.255.0", "Subnet mask"],
                ["gateway", "string", "192.168.0.1", "Default gateway"],
                ["cycle_time_ms", "int", "4", "RT send interval (ms)"],
                ["watchdog_ms", "int", "200", "Output watchdog timeout (ms)"],
            ],
            S,
            col_widths=[c1, c2, c3, c4],
        ),
        Paragraph("beckhoff.*  Keys", S["h2"]),
        _table(
            ["Key", "Type", "Default", "Description"],
            [
                ["enabled", "bool", "false", "Enable Beckhoff ADS"],
                ["ams_net_id", "string", "5.80.201.232.1.1", "TwinCAT AMS Net ID"],
                ["ams_port", "int", "851", "ADS port (851=Runtime 1)"],
                ["symbol_name", "string", "GVL.stQC", "PLC symbol for exchange struct"],
                ["poll_interval_ms", "int", "50", "Poll cycle (ms)"],
                ["reconnect_delay", "float", "3.0", "Initial reconnect delay (s)"],
                ["reconnect_max", "float", "30.0", "Max reconnect delay (s)"],
            ],
            S,
            col_widths=[c1, c2, c3, c4],
        ),
        Paragraph("auth.*  Keys", S["h2"]),
        _table(
            ["Key", "Type", "Default", "Description"],
            [
                ["active_directory_enabled", "bool", "true", "Enable LDAP/AD authentication"],
                ["login_required", "bool", "true", "Require login on startup"],
                ["no_auth_default_role", "string", "ADMIN", "Role when AD is disabled"],
                ["ldap_servers", "list", '["dc1.example.com"]', "AD domain controller hostnames"],
                ["ldap_domain", "string", "example.com", "AD domain FQDN"],
                ["ldap_base_dn", "string", "DC=example,DC=com", "LDAP search base DN"],
                ["ldap_group_role_map", "dict", "(see below)", "AD group \u2192 role mapping"],
                ["ldap_default_role", "string", "OPERATOR", "Role when no group matches"],
                ["ldap_use_tls", "bool", "false", "Enable STARTTLS (port 389)"],
                ["ldap_use_ssl", "bool", "false", "Enable LDAPS (port 636)"],
                ["ldap_connect_timeout", "float", "5.0", "TCP connect timeout (s)"],
            ],
            S,
            col_widths=[c1, c2, c3, c4],
        ),
        Spacer(1, 10 * mm),
        _hr(),
        Paragraph(
            f"QC System User &amp; Installation Manual &mdash; Version 1.0 &mdash; "
            f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            S["footer"],
        ),
        Paragraph(
            "Confidential. Unauthorised reproduction or distribution is prohibited.",
            S["footer"],
        ),
    ]


# ===========================================================================
# Page template callbacks
# ===========================================================================

def _on_page(canvas, doc):
    """Header and footer drawn on every page (except cover)."""
    canvas.saveState()
    page_num = doc.page

    # Footer
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(_GREY_TEXT)
    canvas.drawCentredString(
        PAGE_W / 2, 12 * mm,
        f"QC System Manual  |  Page {page_num}"
    )

    # Header line
    if page_num > 1:
        canvas.setStrokeColor(_ACCENT_LIGHT)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, PAGE_H - 14 * mm, PAGE_W - MARGIN, PAGE_H - 14 * mm)

    canvas.restoreState()


def _on_first_page(canvas, doc):
    """Cover page — no header/footer."""
    pass


# ===========================================================================
# Main
# ===========================================================================

def generate_manual(output_path: Path | None = None) -> Path:
    """Generate the PDF manual and return the output path."""
    out = output_path or _OUTPUT
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title="QC System — User & Installation Manual",
        author="QC System Development Team",
        subject="Installation, Configuration, and Operation Guide",
    )

    S = _build_styles()

    story = []
    story += _cover(S)
    story += _toc(S)
    story += _section_overview(S)
    story += _section_requirements(S)
    story += _section_installation(S)
    story += _section_first_launch(S)
    story += _section_settings(S)
    story += _section_auth(S)
    story += _section_operation(S)
    story += _section_plc(S)
    story += _section_data(S)
    story += _section_troubleshooting(S)
    story += _section_appendix(S)

    doc.build(story, onFirstPage=_on_first_page, onLaterPages=_on_page)
    print(f"[generate_manual] Manual written to {out}  ({out.stat().st_size // 1024} KB)")
    return out


if __name__ == "__main__":
    generate_manual()
