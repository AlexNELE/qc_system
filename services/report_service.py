"""
services/report_service.py — PDF batch report generator (QThread).

Architecture:
  - ReportService is a QThread.  Call start() after Batch End to generate the
    PDF without blocking the UI thread.
  - On completion it emits report_finished(pdf_path) which MainWindow connects
    to app_signals.report_ready so the UI can display the result.
  - All SQLite access goes through the StorageService read API which is
    thread-safe (connection-per-thread + threading.Lock for writes; reads
    obtain their own connection via threading.local so they never contend with
    write threads).

PDF structure (one file per batch):
  1. Title header       — report title, batch ID, start/end timestamps.
  2. Summary table      — one row per camera + grand-total row.
                          Columns: Camera | Total Frames | OK | MISSING |
                                   Total Detected | Expected/Frame | Status
  3. Missing-item images — grouped by camera.  Each annotated JPEG is embedded
                          at up to PAGE_WIDTH - 2*MARGIN wide.  A caption line
                          below each image shows timestamp, detected count,
                          and expected count.
  4. No-missing notice  — "No missing items detected in this batch." when the
                          missing list is empty.

File path:
  <REPORTS_DIR>/<batch_id>_<YYYYmmdd_HHMMSS>.pdf

FUTURE: Cloud upload — attach the finished PDF to a CloudUploadService after
        report_finished is emitted.
FUTURE: Statistics dashboard — call storage.get_all_camera_batch_stats() and
        plot pass/fail bar charts embedded in the report.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QThread, Signal

import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ReportLayout constants — tune here, nowhere else.
# ---------------------------------------------------------------------------
_PAGE_WIDTH_PT:  float = 595.0   # A4 portrait width in points
_PAGE_HEIGHT_PT: float = 842.0   # A4 portrait height in points
_MARGIN_PT:      float = 40.0    # left/right/top/bottom margin
_USABLE_WIDTH:   float = _PAGE_WIDTH_PT - 2 * _MARGIN_PT

# Maximum image width and height for embedded missing-item thumbnails.
_IMG_MAX_W: float = _USABLE_WIDTH
_IMG_MAX_H: float = 300.0        # points — prevents a single image filling a page

# Summary table column widths (must sum to _USABLE_WIDTH).
_COL_WIDTHS = [60.0, 80.0, 50.0, 60.0, 90.0, 90.0, 55.0]

# Colour palette (RGB 0-1 range for reportlab)
_COLOUR_HEADER     = (0.16, 0.24, 0.35)   # dark navy
_COLOUR_PASS       = (0.18, 0.63, 0.27)   # green
_COLOUR_FAIL       = (0.82, 0.15, 0.18)   # red
_COLOUR_GRAND_TOTAL= (0.90, 0.90, 0.90)   # light grey row fill
_COLOUR_ROW_ALT    = (0.96, 0.96, 0.96)   # alternating row fill
_COLOUR_WHITE      = (1.0, 1.0, 1.0)


class ReportService(QThread):
    """
    Background QThread that generates a PDF batch report.

    Usage::

        svc = ReportService(
            batch_id='BATCH_001',
            batch_start_time=datetime_obj,
            batch_end_time=datetime_obj,
            storage=storage_service_instance,
        )
        svc.report_finished.connect(on_report_done)
        svc.start()

    Signals
    -------
    report_finished(pdf_path: str)
        Emitted in the worker thread but connected via Qt queued connection
        so the slot runs in the main thread.  pdf_path is empty on failure.
    report_error(message: str)
        Emitted if an unrecoverable error prevents PDF generation.
    """

    report_finished = Signal(str)   # pdf_path — empty string on failure
    report_error    = Signal(str)   # human-readable error message

    def __init__(
        self,
        batch_id: str,
        batch_start_time: datetime,
        batch_end_time: datetime,
        storage,                        # StorageService instance
        reports_dir: str = settings.REPORTS_DIR,
        parent=None,
    ) -> None:
        """
        Parameters
        ----------
        batch_id:
            The batch identifier string used to query SQLite.
        batch_start_time:
            datetime when Batch Start was clicked.
        batch_end_time:
            datetime when Batch End was clicked.
        storage:
            StorageService instance.  Called on this thread (safe by design).
        reports_dir:
            Directory where PDF files are written.  Created if absent.
        """
        super().__init__(parent)
        self._batch_id         = batch_id
        self._batch_start_time = batch_start_time
        self._batch_end_time   = batch_end_time
        self._storage          = storage
        self._reports_dir      = reports_dir

    # ------------------------------------------------------------------
    # QThread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Generate the PDF report.  Runs in the worker thread."""
        try:
            pdf_path = self._generate_report()
            self.report_finished.emit(pdf_path)
            logger.info("Report generated: %s", pdf_path)
        except Exception as exc:
            logger.error("Report generation failed: %s", exc, exc_info=True)
            self.report_error.emit(str(exc))
            self.report_finished.emit("")

    # ------------------------------------------------------------------
    # PDF generation (private — called from run())
    # ------------------------------------------------------------------

    def _generate_report(self) -> str:
        """
        Build the full PDF and write it to disk.

        Returns the absolute path of the written file.
        Raises on any unrecoverable error.
        """
        # Lazy import so reportlab is only loaded when a report is actually
        # requested — keeps startup time low on machines that do not need it.
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen.canvas import Canvas

        os.makedirs(self._reports_dir, exist_ok=True)

        ts_file  = self._batch_end_time.strftime("%Y%m%d_%H%M%S")
        filename = f"{self._batch_id}_{ts_file}.pdf"
        pdf_path = os.path.abspath(
            os.path.join(self._reports_dir, filename)
        )

        canvas = Canvas(pdf_path, pagesize=A4)
        canvas.setTitle(f"QC Inspection Report — {self._batch_id}")

        # Fetch data once — both draws use these.
        camera_stats    = self._storage.get_all_camera_batch_stats(self._batch_id)
        missing_records = self._storage.get_batch_defect_records(self._batch_id)

        # ----- Page 1: title header + summary table -----
        y = self._draw_header(canvas, _PAGE_HEIGHT_PT - _MARGIN_PT)
        y = self._draw_summary_table(canvas, y - 20, camera_stats)

        # ----- Missing-item images section -----
        # A heading on the same page if there is room, else a new page.
        y = self._maybe_new_page(canvas, y, needed_height=60)
        y = self._draw_defect_images_section(canvas, y, missing_records)

        canvas.save()
        return pdf_path

    # ------------------------------------------------------------------
    # Section renderers
    # ------------------------------------------------------------------

    def _draw_header(self, canvas, y: float) -> float:
        """
        Draw the report title block.

        Returns the y coordinate immediately below the last drawn element.
        """
        from reportlab.lib.colors import Color

        x = _MARGIN_PT

        # Background banner
        banner_h = 70.0
        canvas.setFillColor(Color(*_COLOUR_HEADER))
        canvas.rect(
            x, y - banner_h,
            _USABLE_WIDTH, banner_h,
            fill=1, stroke=0,
        )

        canvas.setFillColor(Color(1.0, 1.0, 1.0))

        # Title
        canvas.setFont("Helvetica-Bold", 18)
        canvas.drawString(x + 10, y - 28, "QC Inspection Report")

        # Batch ID
        canvas.setFont("Helvetica", 11)
        canvas.drawString(x + 10, y - 48, f"Batch ID: {self._batch_id}")

        # Timestamps — right-aligned
        canvas.setFont("Helvetica", 10)
        start_str = self._batch_start_time.strftime("%Y-%m-%d  %H:%M:%S")
        end_str   = self._batch_end_time.strftime("%Y-%m-%d  %H:%M:%S")
        right_x   = x + _USABLE_WIDTH - 10

        canvas.drawRightString(right_x, y - 30, f"Start:  {start_str}")
        canvas.drawRightString(right_x, y - 48, f"End:    {end_str}")

        return y - banner_h

    def _draw_summary_table(
        self, canvas, y: float, camera_stats: list[dict]
    ) -> float:
        """
        Draw the per-camera summary table and grand-total row.

        Returns y below the table.
        """
        from reportlab.lib.colors import Color, black

        x          = _MARGIN_PT
        row_height = 20.0
        headers    = [
            "Camera", "Frames", "OK", "MISSING",
            "Total Detected", "Expected", "Status",
        ]

        # --- Column header row ---
        canvas.setFillColor(Color(*_COLOUR_HEADER))
        canvas.rect(
            x, y - row_height,
            _USABLE_WIDTH, row_height,
            fill=1, stroke=0,
        )
        canvas.setFillColor(Color(1.0, 1.0, 1.0))
        canvas.setFont("Helvetica-Bold", 9)
        self._draw_table_row_cells(canvas, x, y, row_height, headers, _COL_WIDTHS)

        y -= row_height

        # --- Data rows ---
        grand_frames  = grand_ok = grand_missing = grand_detected = 0
        grand_expected = settings.EXPECTED_COUNT

        for idx, row_data in enumerate(camera_stats):
            cam_id        = row_data.get("camera_id", "?")
            total_frames  = row_data.get("total_frames",  0) or 0
            ok_count      = row_data.get("ok_count",      0) or 0
            missing_count = row_data.get("missing_count",  0) or 0
            total_detected= row_data.get("total_detected",0) or 0
            expected      = row_data.get("expected_count", settings.EXPECTED_COUNT) or settings.EXPECTED_COUNT
            status_str    = "PASS" if missing_count == 0 else "FAIL"

            grand_frames   += total_frames
            grand_ok       += ok_count
            grand_missing  += missing_count
            grand_detected += total_detected

            row_colour = _COLOUR_ROW_ALT if idx % 2 == 1 else _COLOUR_WHITE
            canvas.setFillColor(Color(*row_colour))
            canvas.rect(
                x, y - row_height,
                _USABLE_WIDTH, row_height,
                fill=1, stroke=0,
            )

            # Status badge colour
            status_colour = _COLOUR_PASS if status_str == "PASS" else _COLOUR_FAIL

            canvas.setFillColor(Color(0.1, 0.1, 0.1))
            canvas.setFont("Helvetica", 9)
            cells = [
                str(cam_id),
                str(total_frames),
                str(ok_count),
                str(missing_count),
                f"{total_detected:,}",
                str(expected),
                "",           # status drawn separately as coloured badge
            ]
            self._draw_table_row_cells(canvas, x, y, row_height, cells, _COL_WIDTHS)

            # Draw status as coloured text in the last column
            status_x = x + sum(_COL_WIDTHS[:-1]) + _COL_WIDTHS[-1] / 2
            canvas.setFillColor(Color(*status_colour))
            canvas.setFont("Helvetica-Bold", 9)
            canvas.drawCentredString(status_x, y - row_height + 6, status_str)

            y -= row_height

        # --- Grid lines ---
        canvas.setStrokeColor(black)
        canvas.setLineWidth(0.4)
        table_top = y + len(camera_stats) * row_height + row_height  # re-compute top
        # Vertical lines
        col_x = x
        for w in _COL_WIDTHS:
            canvas.line(col_x, table_top, col_x, y)
            col_x += w
        canvas.line(col_x, table_top, col_x, y)  # rightmost

        # --- Grand total row ---
        grand_status_str = "PASS" if grand_missing == 0 else "FAIL"
        y -= 4  # small gap before grand total
        canvas.setFillColor(Color(*_COLOUR_GRAND_TOTAL))
        canvas.rect(
            x, y - row_height,
            _USABLE_WIDTH, row_height,
            fill=1, stroke=0,
        )
        canvas.setFillColor(Color(0.1, 0.1, 0.1))
        canvas.setFont("Helvetica-Bold", 9)
        grand_cells = [
            "ALL",
            str(grand_frames),
            str(grand_ok),
            str(grand_missing),
            f"{grand_detected:,}",
            str(grand_expected),
            "",
        ]
        self._draw_table_row_cells(canvas, x, y, row_height, grand_cells, _COL_WIDTHS)

        # Grand total status badge
        grand_colour = _COLOUR_PASS if grand_status_str == "PASS" else _COLOUR_FAIL
        grand_x = x + sum(_COL_WIDTHS[:-1]) + _COL_WIDTHS[-1] / 2
        canvas.setFillColor(Color(*grand_colour))
        canvas.drawCentredString(grand_x, y - row_height + 6, grand_status_str)

        y -= row_height
        return y

    def _draw_defect_images_section(
        self, canvas, y: float, missing_records: list[dict]
    ) -> float:
        """
        Draw the missing-item images section, grouped by camera.

        If there are no missing items, writes a notice instead.
        Returns y below the last element drawn.
        """
        from reportlab.lib.colors import Color

        x = _MARGIN_PT

        # Section heading
        y = self._maybe_new_page(canvas, y, needed_height=40)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.setFillColor(Color(*_COLOUR_HEADER))
        canvas.drawString(x, y - 16, "Missing Item Images")
        canvas.setStrokeColor(Color(*_COLOUR_HEADER))
        canvas.setLineWidth(1)
        canvas.line(x, y - 20, x + _USABLE_WIDTH, y - 20)
        y -= 30

        if not missing_records:
            canvas.setFont("Helvetica-Oblique", 11)
            canvas.setFillColor(Color(0.3, 0.3, 0.3))
            canvas.drawString(x, y - 14, "No missing items detected in this batch.")
            y -= 30
            return y

        # Group by camera_id (records already ordered camera_id ASC)
        groups: dict[int, list[dict]] = {}
        for rec in missing_records:
            cid = rec["camera_id"]
            groups.setdefault(cid, []).append(rec)

        for cam_id, records in sorted(groups.items()):
            # Camera sub-heading
            y = self._maybe_new_page(canvas, y, needed_height=40)
            canvas.setFont("Helvetica-Bold", 11)
            canvas.setFillColor(Color(*_COLOUR_HEADER))
            canvas.drawString(x, y - 14, f"Camera {cam_id}")
            y -= 24

            for rec in records:
                annotated_path = rec.get("annotated_path") or rec.get("image_path")
                timestamp_str  = rec.get("timestamp", "")
                detected       = rec.get("detected_count", "?")
                expected       = rec.get("expected_count", settings.EXPECTED_COUNT)

                # Try to embed the annotated image
                if annotated_path and os.path.isfile(annotated_path):
                    img_w, img_h = self._get_scaled_image_dims(annotated_path)
                    y = self._maybe_new_page(canvas, y, needed_height=img_h + 30)
                    try:
                        canvas.drawImage(
                            annotated_path,
                            x, y - img_h,
                            width=img_w, height=img_h,
                            preserveAspectRatio=True,
                        )
                        y -= img_h
                    except Exception as exc:
                        logger.warning(
                            "Could not embed image %s: %s", annotated_path, exc
                        )
                        canvas.setFont("Helvetica-Oblique", 9)
                        canvas.setFillColor(Color(0.6, 0.1, 0.1))
                        canvas.drawString(
                            x, y - 12,
                            f"[Image unavailable: {os.path.basename(annotated_path)}]",
                        )
                        y -= 16
                else:
                    # No image file — show placeholder text
                    y = self._maybe_new_page(canvas, y, needed_height=20)
                    canvas.setFont("Helvetica-Oblique", 9)
                    canvas.setFillColor(Color(0.5, 0.5, 0.5))
                    label = (
                        annotated_path
                        if annotated_path
                        else "(no image path recorded)"
                    )
                    canvas.drawString(x, y - 12, f"[Image not found: {label}]")
                    y -= 16

                # Caption line below image
                y = self._maybe_new_page(canvas, y, needed_height=18)
                canvas.setFont("Helvetica", 8)
                canvas.setFillColor(Color(0.2, 0.2, 0.2))
                caption = (
                    f"Timestamp: {timestamp_str}   "
                    f"Detected: {detected}   Expected: {expected}"
                )
                canvas.drawString(x, y - 12, caption)
                y -= 22

        return y

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_table_row_cells(
        canvas,
        x: float,
        y: float,
        row_height: float,
        cells: list[str],
        col_widths: list[float],
    ) -> None:
        """
        Draw a list of text strings centred in their respective columns.

        Uses the canvas's current font and fill colour.  Each cell string
        is drawn centred horizontally within its column width.
        """
        text_y = y - row_height + 6
        cx = x
        for cell, col_w in zip(cells, col_widths):
            canvas.drawCentredString(cx + col_w / 2, text_y, cell)
            cx += col_w

    def _maybe_new_page(self, canvas, y: float, needed_height: float) -> float:
        """
        Start a new page if there is not enough vertical space remaining.

        Parameters
        ----------
        canvas:         Active reportlab Canvas.
        y:              Current drawing y-coordinate.
        needed_height:  Minimum points needed for the next element.

        Returns
        -------
        New y coordinate (unchanged if no page break, else near top).
        """
        if y - needed_height < _MARGIN_PT:
            canvas.showPage()
            y = _PAGE_HEIGHT_PT - _MARGIN_PT
        return y

    @staticmethod
    def _get_scaled_image_dims(path: str) -> tuple[float, float]:
        """
        Compute scaled (width, height) in points for an image file so it
        fits within _IMG_MAX_W x _IMG_MAX_H while preserving aspect ratio.

        Falls back to (_IMG_MAX_W, _IMG_MAX_H) if the image cannot be read.
        """
        try:
            import cv2
            img = cv2.imread(path)
            if img is None:
                return (_IMG_MAX_W, _IMG_MAX_H)
            h_px, w_px = img.shape[:2]
            if w_px == 0 or h_px == 0:
                return (_IMG_MAX_W, _IMG_MAX_H)

            scale_w = _IMG_MAX_W / w_px
            scale_h = _IMG_MAX_H / h_px
            scale   = min(scale_w, scale_h, 1.0)   # never upscale
            return (w_px * scale, h_px * scale)
        except Exception:
            return (_IMG_MAX_W, _IMG_MAX_H)
