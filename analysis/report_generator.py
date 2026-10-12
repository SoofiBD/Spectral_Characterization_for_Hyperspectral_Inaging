"""PDF report generation with ReportLab."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from reportlab.lib.utils import ImageReader


def _fmt(value, unit="nm", digits=3) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{value:.{digits}f} {unit}".strip()


def _table(rows, col_widths):
    t = Table(rows, colWidths=col_widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2a36")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa5b1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f6")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def generate_report(path: str | Path, params: dict, summary: dict,
                    plot_images: list[tuple[str, str]], csv_path: str | None = None,
                    device_info: dict | None = None) -> Path:
    """plot_images: list of (title, png_path)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=20, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=colors.HexColor("#1f2a36"))
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8,
                           textColor=colors.HexColor("#555555"))

    story = [Paragraph("Spectral Characterization Report", h1),
             Paragraph(f"Generated {dt.datetime.now():%Y-%m-%d %H:%M:%S}", small),
             Spacer(1, 6 * mm)]

    story.append(Paragraph("Measurement parameters", h2))
    rows = [["Parameter", "Value"],
            ["Range", f"{params['start']:g}-{params['stop']:g} nm"],
            ["Step", f"{params['step']:g} nm"],
            ["Averages per point", str(params["averages"])],
            ["Integration time", f"{params['integration_ms']:g} ms"],
            ["Entrance slit", f"{params['entrance_slit_um']:g} µm" if params.get("entrance_slit_um") else "not recorded"],
            ["Exit slit", f"{params['exit_slit_um']:g} µm" if params.get("exit_slit_um") else "not recorded"],
            ["Settling time", f"{params.get('settle_s', 0):g} s"],
            ["Backward scan", "Yes" if params.get("backward") else "No"],
            ["Background (dark) correction", str(params.get("dark", "none"))],
            ["Mode", "Simulation" if params.get("simulation") else "Hardware"]]
    for k, v in (device_info or {}).items():
        rows.append([k, v])
    if csv_path:
        rows.append(["Data file", Path(csv_path).name])
    story += [_table(rows, [55 * mm, 110 * mm]), Spacer(1, 6 * mm)]

    story.append(Paragraph("Results", h2))
    s = summary
    res = [["Metric", "Value"],
           ["Number of points", str(s.get("n_points", "n/a"))],
           ["Mean wavelength error", _fmt(s.get("mean_error_nm"))],
           ["Maximum |wavelength error|", _fmt(s.get("max_abs_error_nm"))],
           ["RMSE", _fmt(s.get("rmse_nm"))],
           ["Mean absolute error", _fmt(s.get("mae_nm"))],
           ["Std. dev. of error", _fmt(s.get("std_error_nm"))],
           ["Mean FWHM", _fmt(s.get("mean_fwhm_nm"))],
           ["Minimum FWHM", _fmt(s.get("min_fwhm_nm"))],
           ["Maximum FWHM", _fmt(s.get("max_fwhm_nm"))],
           ["Repeatability (mean σ of peak)", _fmt(s.get("repeatability_mean_std_nm"), digits=4)],
           ["Repeatability (max σ of peak)", _fmt(s.get("repeatability_max_std_nm"), digits=4)],
           ["Mean SNR", _fmt(s.get("mean_snr"), unit="", digits=1)]]
    if "hysteresis_mean_nm" in s:
        res += [["Fwd-Bwd difference (mean)", _fmt(s.get("hysteresis_mean_nm"))],
                ["Fwd-Bwd difference (max |Δ|)", _fmt(s.get("hysteresis_max_nm"))]]
    story.append(_table(res, [75 * mm, 90 * mm]))

    max_w = 170 * mm
    for i, (title, png) in enumerate(plot_images):
        if not Path(png).exists():
            continue
        if i % 2 == 0:
            story.append(PageBreak())
        iw, ih = ImageReader(png).getSize()
        w = max_w
        h = w * ih / iw
        if h > 110 * mm:
            h = 110 * mm
            w = h * iw / ih
        story += [Paragraph(title, h2), Image(png, width=w, height=h), Spacer(1, 4 * mm)]

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title="Spectral Characterization Report")
    doc.build(story)
    return path
