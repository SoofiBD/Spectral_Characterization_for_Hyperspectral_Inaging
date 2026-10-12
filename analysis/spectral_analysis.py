"""Spectral line analysis: peak position, FWHM, intensities, SNR and summary statistics."""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd
from scipy.integrate import trapezoid
from scipy.ndimage import median_filter


CSV_COLUMNS = ["set_wavelength", "measured_peak", "error_nm", "fwhm_nm",
               "peak_intensity", "integrated_intensity", "snr"]


@dataclass
class LineResult:
    set_wavelength: float
    measured_peak: float
    error_nm: float
    fwhm_nm: float
    peak_intensity: float
    integrated_intensity: float
    snr: float

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_baseline(intensity: np.ndarray) -> tuple[float, float]:
    """Baseline and off-line noise for a spectrum dominated by one narrow line."""
    baseline = float(np.median(intensity))
    noise = float(1.4826 * np.median(np.abs(intensity - baseline)))
    return baseline, max(noise, 1e-9)


def _half_max_crossing(x: np.ndarray, y: np.ndarray, i0: int, i1: int, half: float) -> float:
    """Linear interpolation of the half-max crossing between samples i0 and i1."""
    y0, y1 = y[i0], y[i1]
    if y1 == y0:
        return float(x[i0])
    return float(x[i0] + (half - y0) * (x[i1] - x[i0]) / (y1 - y0))


def analyze_spectrum(wavelength: np.ndarray, intensity: np.ndarray,
                     set_wavelength: float | None = None,
                     search_window_nm: float = 30.0) -> LineResult:
    wl = np.asarray(wavelength, dtype=float)
    raw = np.asarray(intensity, dtype=float)
    baseline, noise = estimate_baseline(raw)
    y = raw - baseline

    # Restrict the search to a window around the set wavelength (avoids 2nd order / stray peaks)
    if set_wavelength is not None:
        mask = np.abs(wl - set_wavelength) <= search_window_nm
        if mask.sum() < 5:
            mask = np.ones_like(wl, dtype=bool)
    else:
        mask = np.ones_like(wl, dtype=bool)
    idx = np.flatnonzero(mask)
    smooth = median_filter(y, size=3)
    i_peak = int(idx[np.argmax(smooth[idx])])
    peak_val = float(y[i_peak])

    nan_result = LineResult(set_wavelength if set_wavelength is not None else np.nan,
                            np.nan, np.nan, np.nan, peak_val, np.nan, peak_val / noise)
    if peak_val <= 3 * noise or i_peak in (0, wl.size - 1):
        return nan_result

    # Sub-pixel peak: parabolic fit on log intensity (exact for Gaussian lines)
    ya, yb, yc = y[i_peak - 1], y[i_peak], y[i_peak + 1]
    if min(ya, yb, yc) > 0:
        la, lb, lc = np.log([ya, yb, yc])
        denom = la - 2 * lb + lc
        delta = 0.5 * (la - lc) / denom if denom != 0 else 0.0
    else:
        denom = ya - 2 * yb + yc
        delta = 0.5 * (ya - yc) / denom if denom != 0 else 0.0
    delta = float(np.clip(delta, -0.5, 0.5))
    step = wl[i_peak + 1] - wl[i_peak] if delta >= 0 else wl[i_peak] - wl[i_peak - 1]
    peak_wl = float(wl[i_peak] + delta * step)

    # FWHM by interpolated half-maximum crossings
    half = peak_val / 2.0
    left = i_peak
    while left > 0 and y[left] > half:
        left -= 1
    right = i_peak
    while right < y.size - 1 and y[right] > half:
        right += 1
    if left == 0 or right == y.size - 1:
        fwhm = np.nan
        x_lo, x_hi = wl[max(i_peak - 5, 0)], wl[min(i_peak + 5, wl.size - 1)]
    else:
        x_lo = _half_max_crossing(wl, y, left, left + 1, half)
        x_hi = _half_max_crossing(wl, y, right - 1, right, half)
        fwhm = x_hi - x_lo

    # Integrated intensity within +/- 1.5 FWHM of the centre (~ 99.9 % of a Gaussian)
    half_width = 1.5 * fwhm if np.isfinite(fwhm) else 5.0
    imask = np.abs(wl - peak_wl) <= half_width
    integrated = float(trapezoid(y[imask], wl[imask])) if imask.sum() > 1 else np.nan

    set_wl = set_wavelength if set_wavelength is not None else np.nan
    return LineResult(
        set_wavelength=float(set_wl),
        measured_peak=peak_wl,
        error_nm=peak_wl - set_wl if set_wavelength is not None else np.nan,
        fwhm_nm=float(fwhm),
        peak_intensity=peak_val,
        integrated_intensity=integrated,
        snr=peak_val / noise,
    )


def peak_repeatability(wavelength: np.ndarray, scans: list[np.ndarray],
                       set_wavelength: float) -> tuple[float, float]:
    """Mean and standard deviation of peak positions over individual (non-averaged) scans."""
    peaks = [analyze_spectrum(wavelength, s, set_wavelength).measured_peak for s in scans]
    peaks = np.array([p for p in peaks if np.isfinite(p)])
    if peaks.size == 0:
        return np.nan, np.nan
    return float(peaks.mean()), float(peaks.std(ddof=1)) if peaks.size > 1 else 0.0


def summarize(df: pd.DataFrame) -> dict:
    """Summary statistics for the report. Uses the forward scan if a direction column exists."""
    if df.empty:
        return {}
    fwd = df[df["direction"] == "forward"] if "direction" in df else df
    err = fwd["error_nm"].dropna().to_numpy()
    fwhm = fwd["fwhm_nm"].dropna().to_numpy()
    out = {
        "n_points": int(len(fwd)),
        "mean_error_nm": float(np.mean(err)) if err.size else np.nan,
        "max_abs_error_nm": float(np.max(np.abs(err))) if err.size else np.nan,
        "rmse_nm": float(np.sqrt(np.mean(err ** 2))) if err.size else np.nan,
        "mae_nm": float(np.mean(np.abs(err))) if err.size else np.nan,
        "std_error_nm": float(np.std(err, ddof=1)) if err.size > 1 else np.nan,
        "mean_fwhm_nm": float(np.mean(fwhm)) if fwhm.size else np.nan,
        "min_fwhm_nm": float(np.min(fwhm)) if fwhm.size else np.nan,
        "max_fwhm_nm": float(np.max(fwhm)) if fwhm.size else np.nan,
        "mean_snr": float(fwd["snr"].mean()),
    }
    if "peak_std_nm" in fwd:
        std = fwd["peak_std_nm"].dropna()
        out["repeatability_mean_std_nm"] = float(std.mean()) if len(std) else np.nan
        out["repeatability_max_std_nm"] = float(std.max()) if len(std) else np.nan
    if "direction" in df and (df["direction"] == "backward").any():
        f = df[df["direction"] == "forward"].set_index("set_wavelength")["measured_peak"]
        b = df[df["direction"] == "backward"].set_index("set_wavelength")["measured_peak"]
        diff = (f - b).dropna()
        out["hysteresis_mean_nm"] = float(diff.mean()) if len(diff) else np.nan
        out["hysteresis_max_nm"] = float(diff.abs().max()) if len(diff) else np.nan
    return out


def relative_response(df: pd.DataFrame, column: str = "integrated_intensity") -> pd.DataFrame:
    fwd = df[df["direction"] == "forward"] if "direction" in df else df
    out = fwd[["set_wavelength", column]].dropna().copy()
    peak = out[column].max()
    out["relative"] = out[column] / peak if peak and peak > 0 else np.nan
    return out
