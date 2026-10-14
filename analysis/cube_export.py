"""Verified RAW16 export; calibration never modifies acquisition files."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import numpy as np
from acquisition.session import load_verified, utc_now

CUBE_VERSION = "2"
SETTING_KEYS = ("exposure_us", "gain", "offset", "bin", "roi", "format")


def _settings(value):
    return {key: value[key] for key in SETTING_KEYS}


def _temperature(a, b, tolerance):
    if a is not None and b is not None:
        if not np.isfinite(a) or not np.isfinite(b) or abs(a - b) > tolerance:
            raise ValueError("Reference temperature does not match frame; retake reference")


def _band(root, manifest, point, temperature_tolerance_c):
    rec = point["camera"]
    settings = _settings(manifest["camera_settings"])
    if _settings(rec.get("settings", manifest["camera_settings"])) != settings:
        raise ValueError("Frame settings do not match session")
    raw = np.load(root / rec["file"], allow_pickle=False)
    if raw.dtype != np.dtype("uint16") or raw.ndim != 2:
        raise ValueError("Camera frame must be a 2D RAW16 uint16 array")
    if rec["exposure_us"] != settings["exposure_us"]:
        raise ValueError("Frame exposure does not match session")
    band = raw.astype(np.float32)
    ref = manifest["references"].get("camera_dark")
    if ref is not None:
        if _settings(ref["settings"]) != settings:
            raise ValueError("Camera dark settings do not match frames")
        dark = np.load(root / ref["file"], allow_pickle=False)
        if dark.dtype != raw.dtype or dark.shape != raw.shape or ref["exposure_us"] != rec["exposure_us"]:
            raise ValueError("Camera dark shape/exposure does not match frames")
        _temperature(ref.get("temperature_c"), rec.get("temperature_c"), temperature_tolerance_c)
        band -= dark.astype(np.float32)
    return band


def export_envi(session_dir: Path, out_dir: Path, direction: str = "forward",
                white_session: Path | None = None, temperature_tolerance_c: float = 2.0) -> Path:
    if not np.isfinite(temperature_tolerance_c) or temperature_tolerance_c < 0:
        raise ValueError("Invalid reference temperature tolerance")
    session_dir, out_dir = Path(session_dir).resolve(), Path(out_dir).resolve()
    sources = [session_dir] + ([Path(white_session).resolve()] if white_session else [])
    if any(out_dir == p or p in out_dir.parents for p in sources):
        raise ValueError("Export directory must be outside acquisition directories")
    manifest, points, problems = load_verified(session_dir)
    if any(p.startswith("reference camera_dark:") for p in problems):
        raise ValueError("Camera dark failed verification")
    points = sorted((p for p in points if p["direction"] == direction and "camera" in p),
                    key=lambda p: p["target_nm"])
    if not points:
        raise ValueError(f"No verified camera frames for direction '{direction}'")
    white = {}
    wm = None
    if white_session:
        white_session = Path(white_session).resolve()
        wm, wp, issues = load_verified(white_session)
        if issues or wm["status"] != "complete" or wm["parameters"].get("kind") != "white":
            raise ValueError("White reference must be a complete, verified white-reference run")
        if _settings(wm["camera_settings"]) != _settings(manifest["camera_settings"]):
            raise ValueError("White reference settings do not match sample")
        for key in ("serial", "model", "simulated"):
            if wm["devices"]["camera"].get(key) != manifest["devices"]["camera"].get(key):
                raise ValueError("White reference camera identity does not match sample")
        if "camera_dark" not in wm["references"] or "camera_dark" not in manifest["references"]:
            raise ValueError("White correction requires dark references for both runs")
        white = {p["target_nm"]: p for p in wp if p["direction"] == direction and "camera" in p}
        if any(p["target_nm"] not in white for p in points):
            raise ValueError("White reference is missing a sample wavelength")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("cube.hdr", "cube.json"):
        (out_dir / name).unlink(missing_ok=True)
    shape = None
    invalid = 0
    temporary = out_dir / "cube.raw.tmp"
    try:
        with temporary.open("wb") as f:
            for pt in points:
                band = _band(session_dir, manifest, pt, temperature_tolerance_c)
                shape = shape or band.shape
                if band.shape != shape:
                    raise ValueError("Frame shape changed")
                if wm is not None:
                    ref = white[pt["target_nm"]]
                    flat = _band(white_session, wm, ref, temperature_tolerance_c)
                    if flat.shape != band.shape:
                        raise ValueError("White reference shape does not match sample")
                    _temperature(ref["camera"].get("temperature_c"), pt["camera"].get("temperature_c"),
                                 temperature_tolerance_c)
                    raw_white = np.load(white_session / ref["camera"]["file"], allow_pickle=False)
                    maximum = wm["devices"]["camera"].get("max_intensity")
                    if maximum is None or not np.isfinite(maximum) or not 0 < maximum <= 65535:
                        raise ValueError("White reference driver must report its RAW16 saturation level")
                    if np.any(raw_white >= maximum):
                        raise ValueError("White reference is saturated")
                    valid = np.isfinite(flat) & (flat > 0)
                    invalid += int(np.count_nonzero(~valid))
                    band = np.divide(band, flat, out=np.full_like(band, np.nan), where=valid)
                f.write(band.astype("<f4").tobytes())
        temporary.replace(out_dir / "cube.raw")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    h, w = shape
    wavelengths = ", ".join(f"{p['target_nm']:g}" for p in points)
    (out_dir / "cube.hdr").write_text(
        "ENVI\n" + f"description = {{spectral cube v{CUBE_VERSION} from {session_dir.name}}}\n"
        f"samples = {w}\nlines = {h}\nbands = {len(points)}\nheader offset = 0\n"
        "file type = ENVI Standard\ndata type = 4\ninterleave = bsq\nbyte order = 0\n"
        f"wavelength units = Nanometers\nwavelength = {{{wavelengths}}}\n")
    (out_dir / "cube.json").write_text(json.dumps({
        "cube_version": CUBE_VERSION, "created_utc": utc_now(), "session": str(session_dir),
        "manifest_sha256": hashlib.sha256((session_dir / "manifest.json").read_bytes()).hexdigest(),
        "direction": direction, "dark_subtracted": "camera_dark" in manifest["references"],
        "dark_reference": manifest["references"].get("camera_dark"),
        "white_session": str(white_session) if white_session else None,
        "white_manifest_sha256": hashlib.sha256((white_session / "manifest.json").read_bytes()).hexdigest() if wm else None,
        "units": "relative reflectance" if wm else "counts", "invalid_white_pixels": invalid,
        "temperature_tolerance_c": temperature_tolerance_c,
        "camera_settings": manifest["camera_settings"],
        "bands": [{"target_nm": p["target_nm"], "reported_nm": p["reported_nm"],
                   "file": p["camera"]["file"], "sha256": p["camera"]["sha256"]} for p in points],
        "skipped_problems": problems}, indent=2))
    return out_dir / "cube.hdr"
