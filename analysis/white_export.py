"""Export dark/white-normalized spectrometer spectra to a separate directory."""
import json
from pathlib import Path
import numpy as np
from acquisition.session import load_verified, save_spectra, file_record, utc_now


def _corrected(root, manifest, point):
    ref = manifest['references'].get('hdx_dark')
    if ref is None or 'hdx_dark' not in point.get('references', []):
        raise ValueError('White correction requires a matched spectrometer dark for both runs')
    with np.load(root / point['hdx']['file'], allow_pickle=False) as z:
        axis, scans = z['wavelength_nm'], z['raw_scans']
    with np.load(root / ref['file'], allow_pickle=False) as z:
        if not np.array_equal(axis, z['wavelength_nm']) or z['mean_raw'].shape != axis.shape:
            raise ValueError('Spectrometer dark wavelength axis/shape mismatch')
        dark = z['mean_raw']
    if ref['integration_ms'] != point['hdx']['integration_ms']:
        raise ValueError('Spectrometer dark integration mismatch')
    return axis, scans.mean(axis=0) - dark, scans


def export_white_spectra(session_dir: Path, white_dir: Path, out_dir: Path):
    sample, white, output = (Path(p).resolve() for p in (session_dir, white_dir, out_dir))
    if any(output == p or p in output.parents for p in (sample, white)):
        raise ValueError('Export directory must be outside acquisition directories')
    sm, sp, errors = load_verified(sample)
    wm, wp, white_errors = load_verified(white)
    if errors or white_errors or wm['status'] != 'complete' or wm['parameters'].get('kind') != 'white':
        raise ValueError('White normalization requires intact runs and a complete white reference')
    if sm['devices']['spectrometer']['info'] != wm['devices']['spectrometer']['info']:
        raise ValueError('White reference spectrometer identity mismatch')
    if sm['devices']['spectrometer']['simulated'] != wm['devices']['spectrometer']['simulated']:
        raise ValueError('White reference device mode mismatch')
    lookup = {(p['direction'], p['target_nm']): p for p in wp if 'hdx' in p}
    prepared = []
    for point in sp:
        if 'hdx' not in point:
            continue
        ref = lookup.get((point['direction'], point['target_nm']))
        if ref is None:
            raise ValueError('White reference wavelength/direction missing')
        if point['hdx']['integration_ms'] != ref['hdx']['integration_ms']:
            raise ValueError('White reference integration mismatch')
        axis, signal, _ = _corrected(sample, sm, point)
        white_axis, denominator, raw_white = _corrected(white, wm, ref)
        if not np.array_equal(axis, white_axis):
            raise ValueError('White reference wavelength calibration mismatch')
        saturation = wm['devices']['spectrometer'].get('max_intensity', 65535)
        if np.any(raw_white >= saturation):
            raise ValueError('White reference spectrum is saturated')
        valid = np.isfinite(denominator) & (denominator > 0)
        ratio = np.divide(signal, denominator, out=np.full_like(signal, np.nan), where=valid)
        prepared.append((point, ref, axis, ratio, int(np.count_nonzero(~valid))))
    if not prepared:
        raise ValueError('No verified spectrometer measurements')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for point, ref, axis, ratio, invalid in prepared:
        name = f"{point['index']:04d}_{point['direction']}_{point['target_nm']:g}nm.npz"
        save_spectra(output / name, wavelength_nm=axis, relative_signal=ratio)
        records.append({**file_record(output, name), 'source': point['hdx'], 'white': ref['hdx'],
                        'invalid_denominator_pixels': invalid})
    (output / 'normalization.json').write_text(json.dumps({
        'created_utc': utc_now(), 'units': 'relative signal ratio',
        'formula': '(sample - sample_dark) / (white - white_dark)',
        'sample_manifest': file_record(sample, 'manifest.json'),
        'white_manifest': file_record(white, 'manifest.json'),
        'sample_session': str(sample), 'white_session': str(white), 'spectra': records}, indent=2))
    return output
