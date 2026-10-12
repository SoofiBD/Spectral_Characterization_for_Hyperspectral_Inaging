"""Build an ENVI cube from the verified RAW16 camera frames of a session (re-runnable, read-only
on the raw data)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from acquisition.session import load_verified, utc_now

CUBE_VERSION = "1"
SETTING_KEYS = ("exposure_us", "gain", "offset", "bin", "roi", "format")


def export_envi(session_dir: Path, out_dir: Path, direction: str = "forward") -> Path:
    """Write <out_dir>/cube.raw (float32 BSQ) + cube.hdr. Bands are the verified points of one
    scan direction, sorted by wavelength. The camera dark is subtracted only when its settings
    match the frames exactly; a mismatched dark raises instead of being applied."""
    manifest, points, problems = load_verified(session_dir)
    points = sorted((p for p in points if p["direction"] == direction and "camera" in p),
                    key=lambda p: p["target_nm"])
    if not points:
        raise ValueError(f"No verified camera frames for direction '{direction}'")
    settings = {k: manifest["camera_settings"][k] for k in SETTING_KEYS}
    dark = None
    ref = manifest["references"].get("camera_dark")
    if ref is not None:
        if any(p.startswith("reference camera_dark") for p in problems):
            raise ValueError("Camera dark failed verification")
        ref_settings = {k: ref["settings"].get(k) for k in SETTING_KEYS}
        if ref_settings != settings:
            raise ValueError(f"Camera dark settings {ref_settings} do not match frames {settings}")
        dark = np.load(session_dir / ref["file"]).astype(np.float32)

    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("cube.hdr", "cube.json"):  # a failed export must not leave a header for a bad cube
        (out_dir / name).unlink(missing_ok=True)
    first = np.load(session_dir / points[0]["camera"]["file"])
    h, w = first.shape
    with (out_dir / "cube.raw").open("wb") as f:
        for p in points:
            band = np.load(session_dir / p["camera"]["file"]).astype(np.float32)
            if band.shape != (h, w):
                raise ValueError(f"Frame {p['camera']['file']} has shape {band.shape}, expected {(h, w)}")
            if dark is not None and (dark.shape != band.shape or
                                     ref["exposure_us"] != p["camera"]["exposure_us"]):
                raise ValueError(f"Camera dark ({dark.shape}, {ref['exposure_us']} us) does not match "
                                 f"{p['camera']['file']} ({band.shape}, {p['camera']['exposure_us']} us)")
            if dark is not None:
                band -= dark
            f.write(band.astype("<f4").tobytes())
    wavelengths = ", ".join(f"{p['target_nm']:g}" for p in points)
    (out_dir / "cube.hdr").write_text(
        "ENVI\n"
        f"description = {{lamda cube v{CUBE_VERSION} from {session_dir.name}}}\n"
        f"samples = {w}\nlines = {h}\nbands = {len(points)}\nheader offset = 0\n"
        "file type = ENVI Standard\ndata type = 4\ninterleave = bsq\nbyte order = 0\n"
        f"wavelength units = Nanometers\nwavelength = {{{wavelengths}}}\n")
    (out_dir / "cube.json").write_text(json.dumps({
        "cube_version": CUBE_VERSION, "created_utc": utc_now(), "session": session_dir.name,
        "direction": direction, "dark_subtracted": dark is not None, "camera_settings": settings,
        "bands": [{"target_nm": p["target_nm"], "reported_nm": p["reported_nm"],
                   "file": p["camera"]["file"], "sha256": p["camera"]["sha256"]} for p in points],
        "skipped_problems": problems}, indent=2))
    return out_dir / "cube.hdr"
