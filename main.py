"""
main.py — Application entry point.

Sets up:
  1. Logging (rotating file handler + console handler).
  2. Sys path so all sub-packages resolve correctly when run from the
     project root and when bundled by PyInstaller.
  3. QApplication with dark palette.
  4. MainWindow and event loop.

Usage::

    python main.py

PyInstaller bundle::

    pyinstaller QCSystem.spec
    dist/QCSystem/QCSystem.exe   (Windows)
    dist/QCSystem/QCSystem       (Linux)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys


def _setup_logging() -> None:
    """
    Configure root logger with:
      - RotatingFileHandler → logs/qc_system.log  (5 MB × 3 rotations)
      - StreamHandler       → stderr

    Per-camera loggers inherit from root automatically.
    """
    import settings  # imported here so sys.path is already patched

    os.makedirs(settings.LOG_DIR, exist_ok=True)
    log_path = os.path.join(settings.LOG_DIR, "qc_system.log")

    numeric_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-24s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    root.addHandler(ch)


def _patch_syspath() -> None:
    """
    Ensure the project root (directory containing main.py) is on sys.path.

    When bundled by PyInstaller sys._MEIPASS points to the extracted temp
    directory; we add it as well so bundled data files are found correctly.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # PyInstaller _MEIPASS support
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and meipass not in sys.path:
            sys.path.insert(0, meipass)


_APPLE_DARK_QSS = """
/* =================================================================
   Apple macOS Dark Mode  —  QC Inspection System
   ================================================================= */

QMenuBar {
    background-color: #2C2C2E;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    padding: 2px 4px;
    font-size: 13px;
}
QMenuBar::item {
    background: transparent;
    padding: 4px 10px;
    border-radius: 5px;
}
QMenuBar::item:selected { background-color: rgba(255,255,255,0.10); }

QMenu {
    background-color: #2C2C2E;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px;
    padding: 4px 0px;
}
QMenu::item {
    padding: 6px 16px;
    border-radius: 5px;
    margin: 1px 4px;
    color: #FFFFFF;
}
QMenu::item:selected  { background-color: #0A84FF; }
QMenu::separator {
    height: 1px;
    background: rgba(255,255,255,0.10);
    margin: 4px 0px;
}

/* --- Buttons (default — glass pill) --- */
QPushButton {
    background-color: rgba(255,255,255,0.10);
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 0px 16px;
    font-size: 13px;
    font-weight: 600;
    min-width: 64px;
}
QPushButton:hover   { background-color: rgba(255,255,255,0.17); }
QPushButton:pressed { background-color: rgba(255,255,255,0.07); }
QPushButton:disabled {
    color: rgba(255,255,255,0.25);
    background-color: rgba(255,255,255,0.05);
}

/* Batch Start — Apple system blue */
QPushButton#btn_batch_start              { background-color: #0A84FF; }
QPushButton#btn_batch_start:hover        { background-color: #3395FF; }
QPushButton#btn_batch_start:pressed      { background-color: #006FD6; }
QPushButton#btn_batch_start:disabled {
    background-color: rgba(10,132,255,0.25);
    color: rgba(255,255,255,0.35);
}

/* Batch End — Apple system red */
QPushButton#btn_batch_end                { background-color: #FF453A; }
QPushButton#btn_batch_end:hover          { background-color: #FF6B61; }
QPushButton#btn_batch_end:pressed        { background-color: #D93025; }
QPushButton#btn_batch_end:disabled {
    background-color: rgba(255,69,58,0.25);
    color: rgba(255,255,255,0.35);
}

/* Capture All — Apple system green */
QPushButton#btn_capture_all              { background-color: #30D158; font-size: 14px; min-width: 110px; }
QPushButton#btn_capture_all:hover        { background-color: #4DD771; }
QPushButton#btn_capture_all:pressed      { background-color: #25A244; }
QPushButton#btn_capture_all:disabled {
    background-color: rgba(48,209,88,0.25);
    color: rgba(255,255,255,0.35);
}

/* --- Text input --- */
QLineEdit {
    background-color: rgba(255,255,255,0.08);
    color: #FFFFFF;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 8px;
    padding: 0px 10px;
    font-size: 13px;
    selection-background-color: #0A84FF;
}
QLineEdit:focus {
    border: 1.5px solid #0A84FF;
    background-color: rgba(10,132,255,0.08);
}
QLineEdit:read-only {
    background-color: rgba(255,255,255,0.04);
    color: rgba(255,255,255,0.35);
    border: 1px solid rgba(255,255,255,0.06);
}

/* --- Status bar --- */
QStatusBar {
    background-color: #2C2C2E;
    color: #8E8E93;
    border-top: 1px solid rgba(255,255,255,0.08);
    font-size: 12px;
    padding: 0px 6px;
}
QStatusBar QLabel { color: #8E8E93; background: transparent; padding: 0px 4px; }

/* --- Tooltips --- */
QToolTip {
    background-color: #3A3A3C;
    color: #FFFFFF;
    border: 1px solid rgba(255,255,255,0.15);
    border-radius: 7px;
    padding: 5px 8px;
    font-size: 12px;
}

/* --- Scrollbars --- */
QScrollBar:vertical   { background: transparent; width: 8px;  margin: 0; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 0; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: rgba(255,255,255,0.20);
    border-radius: 4px;
    min-height: 20px;
    min-width: 20px;
}
QScrollBar::add-line:vertical,  QScrollBar::sub-line:vertical,
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { height: 0; width: 0; }
"""


def _apply_apple_style(app) -> None:
    """Apply Apple macOS dark-mode palette and stylesheet to the QApplication."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtCore import Qt

    bg      = QColor(0x1C, 0x1C, 0x1E)
    surface = QColor(0x2C, 0x2C, 0x2E)
    text    = QColor(0xFF, 0xFF, 0xFF)
    blue    = QColor(0x0A, 0x84, 0xFF)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          bg)
    palette.setColor(QPalette.ColorRole.WindowText,      text)
    palette.setColor(QPalette.ColorRole.Base,            surface)
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(0x3A, 0x3A, 0x3C))
    palette.setColor(QPalette.ColorRole.ToolTipBase,     surface)
    palette.setColor(QPalette.ColorRole.ToolTipText,     text)
    palette.setColor(QPalette.ColorRole.Text,            text)
    palette.setColor(QPalette.ColorRole.Button,          surface)
    palette.setColor(QPalette.ColorRole.ButtonText,      text)
    palette.setColor(QPalette.ColorRole.BrightText,      Qt.GlobalColor.red)
    palette.setColor(QPalette.ColorRole.Link,            blue)
    palette.setColor(QPalette.ColorRole.Highlight,       blue)
    palette.setColor(QPalette.ColorRole.HighlightedText, text)
    app.setPalette(palette)

    app.setStyleSheet(_APPLE_DARK_QSS)


if __name__ == "__main__":
    _patch_syspath()
    _setup_logging()

    logger = logging.getLogger("main")
    logger.info("=== QC System starting ===")

    # PySide6 must be imported AFTER sys.path is patched
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    # High-DPI support (must be set before QApplication is created)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("QCSystem")
    app.setOrganizationName("IndustrialQC")
    app.setStyle("Fusion")
    _apply_apple_style(app)

    # ------------------------------------------------------------------
    # Authentication — behaviour depends on AUTH_AD_ENABLED in settings.json.
    #
    #   True  → Show login dialog; user must authenticate via AD or the
    #            local offline cache before MainWindow opens.
    #   False → Skip login entirely; an automatic ADMIN session is created
    #            and the application starts immediately without any sign-in.
    # ------------------------------------------------------------------
    import settings as _settings
    import auth

    if _settings.AUTH_AD_ENABLED:
        ldap_svc, user_cache = auth.build_services()
        logger.info("Active Directory enabled — showing login dialog")
        session = auth.show_login(ldap_svc, user_cache)

        if session is None:
            # Operator pressed Exit without logging in — shut down cleanly.
            logger.info("Login cancelled by user — exiting.")
            sys.exit(0)
    else:
        logger.info(
            "Active Directory disabled (AUTH_AD_ENABLED=False) — "
            "starting without authentication"
        )
        session = auth.create_no_auth_session()

    auth.set_session(session)
    logger.info(
        "Authenticated | user=%s role=%s via=%s",
        session.username, session.role.name, session.authenticated_via,
    )

    # ------------------------------------------------------------------
    # Main window
    # ------------------------------------------------------------------
    from ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    logger.info("Event loop started")
    exit_code = app.exec()
    logger.info("Event loop exited with code %d", exit_code)
    sys.exit(exit_code)
