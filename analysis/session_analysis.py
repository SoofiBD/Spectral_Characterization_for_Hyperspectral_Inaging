"""Rebuild derived results from verified sessions without changing acquisition files."""
from pathlib import Path
import numpy as np
from acquisition.session import load_verified
from analysis.spectral_analysis import analyze_spectrum, peak_repeatability


def analyze_raw(wl, raw, target, direction, reported, dark=None, saturation=65535):
    scans = [scan - dark if dark is not None else scan for scan in raw]
    avg = np.mean(scans, axis=0)
    result = analyze_spectrum(wl, avg, target).to_dict()
    mean, std = peak_repeatability(wl, scans, target)
    result.update(direction=direction, peak_mean_nm=mean, peak_std_nm=std,
                  reported_position=reported,
                  saturated_pixels=int(np.count_nonzero(np.asarray(raw) >= saturation)))
    return result, avg


def load_results(root: Path, point_spectra: dict | None = None):
    """point_spectra, if given, receives {row index: (wavelength, averaged spectrum)} for every row."""
    manifest, points, problems = load_verified(root)
    ref = manifest['references'].get('hdx_dark')
    dark, dark_axis = None, None
    if ref and not any(p.startswith('reference hdx_dark:') for p in problems):
        with np.load(root / ref['file'], allow_pickle=False) as z:
            dark, dark_axis = z['mean_raw'], z['wavelength_nm']
    rows, profiles = [], {}
    for point in points:
        if 'hdx' not in point:
            continue
        with np.load(root / point['hdx']['file'], allow_pickle=False) as z:
            wl, raw = z['wavelength_nm'], z['raw_scans']
        applied = dark if 'hdx_dark' in point.get('references', []) else None
        if applied is not None and (not np.array_equal(wl, dark_axis) or
                                   ref['integration_ms'] != point['hdx']['integration_ms']):
            problems.append(f"point {point['index']}: HDX dark axis/integration mismatch")
            continue
        result, avg = analyze_raw(wl, raw, point['target_nm'], point['direction'],
                                  point['reported_nm'], applied,
                                  manifest['devices']['spectrometer'].get('max_intensity', 65535))
        rows.append(result)
        if point_spectra is not None:
            point_spectra[len(rows) - 1] = (wl, avg)
        if point['direction'] == 'forward':
            profiles[point['target_nm']] = (wl, avg)
    return manifest, rows, profiles, problems
