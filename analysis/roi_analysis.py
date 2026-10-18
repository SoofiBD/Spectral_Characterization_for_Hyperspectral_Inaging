"""Two-region (ROI) analysis of verified camera scans.

Boxes are (x0, y0, x1, y1) half-open pixel ranges in the stored RAW16 frame array
(frame[y0:y1, x0:x1]); after binning / sensor ROI, so they are tied to the run's frame geometry.

Uncertainty: every point has one frame, so temporal noise is not measured. The reported
standard error is the pixel-to-pixel scatter inside the region divided by sqrt(n); it is a
lower bound that ignores shot-noise correlation and calibration errors.

Raw files are only read (memory-mapped); exports go to a new directory outside every
acquisition directory and appear atomically (written to a hidden temporary directory first).
"""
from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from acquisition.session import MANIFEST, file_record, load_verified, utc_now
from analysis.cube_export import _settings, _temperature

ROI_VERSION = "1"
MIN_SNR = 3.0          # a denominator below MIN_SNR standard errors gives no ratio
GEOMETRY_KEYS = ("bin", "roi")
Box = tuple[int, int, int, int]


@dataclass
class CameraRun:
    root: Path
    manifest: dict
    points: list[dict]
    problems: list[str]
    dark: np.ndarray
    frames: list[np.ndarray]
    max_intensity: float | None
    temperature_tolerance_c: float
    shape: tuple[int, int] = field(init=False)

    def __post_init__(self):
        self.shape = tuple(self.dark.shape)

    @property
    def kind(self) -> str:
        return self.manifest.get("parameters", {}).get("kind", "sample")

    def geometry(self) -> dict:
        settings = self.manifest["camera_settings"]
        return {"shape": list(self.shape), **{k: settings.get(k) for k in GEOMETRY_KEYS}}


def load_camera_run(root: Path, temperature_tolerance_c: float = 2.0) -> CameraRun:
    """Verified camera points of a run plus its dark; refuses any dark that does not match."""
    if not math.isfinite(temperature_tolerance_c) or temperature_tolerance_c < 0:
        raise ValueError("Invalid reference temperature tolerance")
    root = Path(root).resolve()
    manifest, verified, problems = load_verified(root)
    if any(p.startswith("reference camera_dark:") for p in problems):
        raise ValueError("Camera dark failed verification (missing or checksum mismatch)")
    points = [p for p in verified if "camera" in p]
    if not points:
        raise ValueError("No verified camera frames in this run; check the manifest and recovery warnings")
    ref = manifest.get("references", {}).get("camera_dark")
    if not ref:
        raise ValueError("ROI analysis requires a camera dark recorded with the run")
    dark = np.load(root / ref["file"], mmap_mode="r", allow_pickle=False)
    if dark.dtype != np.uint16 or dark.ndim != 2:
        raise ValueError("Camera dark must be a 2D RAW16 uint16 array")
    session = _settings(manifest["camera_settings"])
    if _settings(ref.get("settings", manifest["camera_settings"])) != session:
        raise ValueError("Camera dark settings do not match the session")
    frames = []
    for point in points:
        cam = point["camera"]
        label = f"frame {point['index']} ({point['target_nm']:g} nm {point['direction']})"
        if "references" in point and "camera_dark" not in point["references"]:
            raise ValueError(f"{label} was recorded before the camera dark")
        if _settings(cam.get("settings", manifest["camera_settings"])) != session:
            raise ValueError(f"{label}: settings do not match the session")
        if cam["exposure_us"] != ref["exposure_us"]:
            raise ValueError(f"{label}: exposure does not match the dark")
        _temperature(ref.get("temperature_c"), cam.get("temperature_c"), temperature_tolerance_c)
        frame = np.load(root / cam["file"], mmap_mode="r", allow_pickle=False)
        if frame.dtype != np.uint16 or frame.shape != dark.shape:
            raise ValueError(f"{label}: shape/dtype does not match the dark")
        frames.append(frame)
    maximum = manifest.get("devices", {}).get("camera", {}).get("max_intensity")
    if maximum is not None and (not np.isfinite(maximum) or not 0 < maximum <= 65535):
        maximum = None
    return CameraRun(root, manifest, points, problems, dark, frames, maximum, temperature_tolerance_c)


def check_boxes(boxes, shape) -> tuple[Box, Box]:
    if len(boxes) != 2:
        raise ValueError("Exactly two regions are required")
    h, w = shape
    out = []
    for box in boxes:
        x0, y0, x1, y1 = (int(v) for v in box)
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise ValueError(f"Region {box} is outside the {w}×{h} frame or empty")
        out.append((x0, y0, x1, y1))
    return tuple(out)


def region_stats(frame, dark, box: Box, max_intensity=None) -> dict:
    """Mean, pixel scatter, standard error and saturated-pixel count of frame - dark in one box."""
    x0, y0, x1, y1 = box
    raw = np.asarray(frame[y0:y1, x0:x1])
    signal = raw.astype(np.float64) - np.asarray(dark[y0:y1, x0:x1], dtype=np.float64)
    n = signal.size
    std = float(signal.std(ddof=1)) if n > 1 else float("nan")
    return {"mean": float(signal.mean()), "std": std, "sem": std / math.sqrt(n) if n > 1 else float("nan"),
            "n": n, "saturated": int(np.count_nonzero(raw >= max_intensity)) if max_intensity else None}


def ratio(a: float, sa: float, b: float, sb: float) -> tuple[float, float, str]:
    """a/b with first-order uncertainty; NaN (with a reason) when the denominator is not significant."""
    if not np.isfinite(b) or b <= 0:
        return float("nan"), float("nan"), "denominator<=0"
    if np.isfinite(sb) and b < MIN_SNR * sb:
        return float("nan"), float("nan"), f"denominator<{MIN_SNR:g}sigma"
    return a / b, math.hypot(sa / b, a * sb / (b * b)), ""


def _flags(*items) -> str:
    return ";".join(i for i in items if i)


def analyze(run: CameraRun, boxes, white: CameraRun | None = None) -> list[dict]:
    """One row per verified frame. With a white run, region means are also divided by the white
    run's dark-subtracted means in the same boxes (relative reflectance; same direction and nm)."""
    a_box, b_box = check_boxes(boxes, run.shape)
    lookup = {}
    if white is not None:
        check_white(run, white)
        lookup = {(p["direction"], p["target_nm"]): (p, f) for p, f in zip(white.points, white.frames)}
    rows = []
    for point, frame in zip(run.points, run.frames):
        sa = region_stats(frame, run.dark, a_box, run.max_intensity)
        sb = region_stats(frame, run.dark, b_box, run.max_intensity)
        r, sr, why = ratio(sa["mean"], sa["sem"], sb["mean"], sb["sem"])
        row = {"index": point["index"], "direction": point["direction"], "target_nm": point["target_nm"],
               "reported_nm": point.get("reported_nm"), "temperature_c": point["camera"].get("temperature_c"),
               "a_mean": sa["mean"], "a_sem": sa["sem"], "a_std": sa["std"], "a_pixels": sa["n"],
               "a_saturated": sa["saturated"],
               "b_mean": sb["mean"], "b_sem": sb["sem"], "b_std": sb["std"], "b_pixels": sb["n"],
               "b_saturated": sb["saturated"],
               "ratio_a_b": r, "ratio_a_b_sem": sr}
        flags = [why and f"ratio:{why}",
                 "a_saturated" if sa["saturated"] else "", "b_saturated" if sb["saturated"] else "",
                 "saturation_unknown" if run.max_intensity is None else ""]
        if white is not None:
            match = lookup.get((point["direction"], point["target_nm"]))
            if match is None:
                raise ValueError(f"White reference has no {point['direction']} frame at {point['target_nm']:g} nm")
            wpoint, wframe = match
            _temperature(wpoint["camera"].get("temperature_c"), point["camera"].get("temperature_c"),
                         run.temperature_tolerance_c)
            values = {}
            for name, box, stats in (("a", a_box, sa), ("b", b_box, sb)):
                ws = region_stats(wframe, white.dark, box, white.max_intensity)
                rel, srel, wwhy = ratio(stats["mean"], stats["sem"], ws["mean"], ws["sem"])
                if ws["saturated"]:
                    rel, srel, wwhy = float("nan"), float("nan"), "white saturated"
                values[name] = (rel, srel)
                row.update({f"{name}_white_mean": ws["mean"], f"{name}_white_saturated": ws["saturated"],
                            f"{name}_reflectance": rel, f"{name}_reflectance_sem": srel})
                flags.append(wwhy and f"{name}_white:{wwhy}")
            (ra, sra), (rb, srb) = values["a"], values["b"]
            wr, swr, wwhy = ratio(ra, sra, rb, srb) if np.isfinite(ra) and np.isfinite(rb) else (
                float("nan"), float("nan"), "reflectance missing")
            row.update(reflectance_ratio_a_b=wr, reflectance_ratio_a_b_sem=swr)
            flags.append(wwhy and f"reflectance_ratio:{wwhy}")
        row["flags"] = _flags(*flags)
        rows.append(row)
    return rows


def forward_backward(rows: list[dict], key: str = "ratio_a_b") -> list[dict]:
    """Backward − forward at every wavelength measured in both directions (matched by set nm)."""
    by = {(r["direction"], r["target_nm"]): r for r in rows}
    out = []
    for nm in sorted({r["target_nm"] for r in rows}):
        f, b = by.get(("forward", nm)), by.get(("backward", nm))
        if f is None or b is None:
            continue
        item = {"target_nm": nm}
        for name in ("a_mean", "b_mean", key):
            fv, bv = f[name], b[name]
            item[f"{name}_forward"], item[f"{name}_backward"] = fv, bv
            item[f"{name}_diff"] = bv - fv
            item[f"{name}_rel_diff"] = (bv - fv) / fv if fv else float("nan")
        sem = math.hypot(f[f"{key}_sem"], b[f"{key}_sem"])
        item[f"{key}_z"] = item[f"{key}_diff"] / sem if sem > 0 else float("nan")
        out.append(item)
    return out


def summary(rows: list[dict], pairs: list[dict], key: str = "ratio_a_b") -> dict:
    def worst(values):
        values = [abs(v) for v in values if v is not None and np.isfinite(v)]
        return max(values) if values else None
    return {"points": len(rows),
            "points_with_flags": sum(bool(r["flags"]) for r in rows),
            "saturated_points": sum(bool(r["a_saturated"] or r["b_saturated"]) for r in rows),
            "invalid_ratio_points": sum(not np.isfinite(r[key]) for r in rows),
            "forward_backward_pairs": len(pairs),
            "max_abs_rel_diff_a": worst(p["a_mean_rel_diff"] for p in pairs),
            "max_abs_rel_diff_b": worst(p["b_mean_rel_diff"] for p in pairs),
            "max_abs_ratio_z": worst(p[f"{key}_z"] for p in pairs)}


def check_white(run: CameraRun, white: CameraRun):
    """Same rules as the ENVI export: complete verified white run, same camera, same settings."""
    if white.root == run.root:
        raise ValueError("The white reference must be a different run")
    if white.problems or white.manifest["status"] != "complete" or white.kind != "white":
        raise ValueError("White reference must be a complete, verified white-reference run")
    if _settings(white.manifest["camera_settings"]) != _settings(run.manifest["camera_settings"]):
        raise ValueError("White reference camera settings do not match the sample")
    for key in ("serial", "model", "simulated"):
        if white.manifest["devices"]["camera"].get(key) != run.manifest["devices"]["camera"].get(key):
            raise ValueError("White reference camera identity does not match the sample")
    if white.shape != run.shape:
        raise ValueError("White reference frame shape does not match the sample")

def roi_preset(run: CameraRun, boxes) -> dict:
    return {"roi_version": ROI_VERSION, "boxes": [list(b) for b in check_boxes(boxes, run.shape)],
            "geometry": run.geometry(), "source_session": str(run.root)}


def load_preset(path: Path, run: CameraRun) -> tuple[Box, Box]:
    """Boxes from a preset (or an exported roi.json); refuses a different frame geometry."""
    data = json.loads(Path(path).read_text())
    data = data.get("preset", data)
    if "boxes" not in data or "geometry" not in data:
        raise ValueError("Not an ROI preset")
    if data["geometry"] != run.geometry():
        raise ValueError(f"ROI preset geometry {data['geometry']} does not match this run {run.geometry()}")
    return check_boxes(data["boxes"], run.shape)


def save_preset(path: Path, run: CameraRun, boxes) -> Path:
    path = Path(path)
    if run.root in path.resolve().parents:
        raise ValueError("Presets cannot be written into an acquisition directory")
    path.write_text(json.dumps(roi_preset(run, boxes), indent=2))
    return path

def _csv(path: Path, rows: list[dict]):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def export(run: CameraRun, boxes, out_dir: Path, white: CameraRun | None = None, extra=None) -> Path:
    """Write roi_signals.csv, roi_forward_backward.csv and roi.json to a new directory.
    extra(tmp_dir) may add files (plots) before the directory appears."""
    out_dir = Path(out_dir).resolve()
    sources = [run.root] + ([white.root] if white else [])
    if any(out_dir == p or p in out_dir.parents for p in sources):
        raise ValueError("Export directory must be outside acquisition directories")
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} already exists")
    rows = analyze(run, boxes, white)
    key = "reflectance_ratio_a_b" if white else "ratio_a_b"
    pairs = forward_backward(rows, key)
    tmp = out_dir.parent / f".{out_dir.name}.partial"
    tmp.mkdir(parents=True)
    try:
        _csv(tmp / "roi_signals.csv", rows)
        _csv(tmp / "roi_forward_backward.csv", pairs)
        info = {
            "roi_version": ROI_VERSION, "created_utc": utc_now(),
            "units": ("dark-subtracted stored RAW16 counts per pixel; *_reflectance = sample/white "
                      "(both dark-subtracted, same boxes)") if white else
                     "dark-subtracted stored RAW16 counts per pixel; no white correction",
            "box_convention": "frame[y0:y1, x0:x1] of the stored frame array (after binning/sensor ROI)",
            "uncertainty": ("*_sem = pixel scatter / sqrt(n) within the region of one frame; ratio errors by "
                            f"first-order propagation; ratio is NaN when the denominator < {MIN_SNR:g} sem"),
            "forward_backward": f"backward - forward of {key} at equal set wavelength; z = diff / combined sem",
            "preset": roi_preset(run, boxes),
            "summary": summary(rows, pairs, key),
            "sample": _provenance(run),
            "white": _provenance(white) if white else None,
            "temperature_tolerance_c": run.temperature_tolerance_c,
        }
        (tmp / "roi.json").write_text(json.dumps(info, indent=2, allow_nan=False, default=_json_default))
        if extra is not None:
            extra(tmp)
        os.rename(tmp, out_dir)
    except BaseException:
        for p in tmp.iterdir():
            p.unlink()
        tmp.rmdir()
        raise
    return out_dir


def _json_default(value):
    raise TypeError(f"Cannot store {type(value).__name__} in roi.json")


def _provenance(run: CameraRun) -> dict:
    m = run.manifest
    return {"session": str(run.root), "manifest": file_record(run.root, MANIFEST), "status": m["status"],
            "kind": run.kind, "camera": m["devices"].get("camera"), "camera_settings": m["camera_settings"],
            "dark_reference": m["references"]["camera_dark"], "max_intensity": run.max_intensity,
            "frames": [{"index": p["index"], "direction": p["direction"], "target_nm": p["target_nm"],
                        "file": p["camera"]["file"], "sha256": p["camera"]["sha256"]} for p in run.points],
            "excluded_problems": run.problems}

def list_runs(data_dir: Path) -> list[dict]:
    """Summarize runs from their manifests without loading or hashing raw files."""
    out = []
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return out
    for path in sorted(data_dir.iterdir(), reverse=True):
        manifest_path = path / MANIFEST
        if not path.is_dir() or path.name.startswith(".") or not manifest_path.is_file():
            continue
        try:
            m = json.loads(manifest_path.read_text())
            points = m.get("points", [])
            camera = [p for p in points if "camera" in p]
            nms = [p["target_nm"] for p in points]
            out.append({"path": path, "name": path.name, "created_utc": m.get("created_utc"),
                        "status": m.get("status"), "kind": m.get("parameters", {}).get("kind", "sample"),
                        "channels": m.get("channels", []), "points": len(points), "camera_points": len(camera),
                        "range_nm": (min(nms), max(nms)) if nms else None,
                        "directions": sorted({p["direction"] for p in points}),
                        "camera_dark": "camera_dark" in m.get("references", {}), "error": None})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            out.append({"path": path, "name": path.name, "status": "unreadable", "error": str(exc),
                        "kind": None, "channels": [], "points": 0, "camera_points": 0, "range_nm": None,
                        "directions": [], "camera_dark": False, "created_utc": None})
    return out
