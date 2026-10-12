"""Session directory + versioned manifest.

Layout (all paths in the manifest are relative to the session root):
    manifest.json            this record, rewritten atomically after every completed point
    dark.npz                 HDX background (raw exposures + mean)
    camera_dark.npy          camera background frame (RAW16), same settings as the scan frames
    NNNN_<dir>_<nm>nm.npz    HDX raw exposures for one point
    NNNN_<dir>_<nm>nm.npy    camera RAW16 frame for one point
    camera.log               camera process stderr

Raw files are never modified after they are recorded; analysis reads them and writes elsewhere.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import numpy as np

MANIFEST = "manifest.json"
FORMAT_VERSION = 2


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save_spectra(path: Path, **arrays):
    """Write an .npz atomically (temp file, fsync, rename) so a crash never leaves half a file."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as output:
        np.savez_compressed(output, **arrays)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def file_record(root: Path, rel: str) -> dict:
    data = (root / rel).read_bytes()
    return {"file": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _check_record(root: Path, rec: dict) -> str | None:
    """Return a problem description, or None when the file matches its record."""
    path = (root / rec["file"]).resolve()
    if root.resolve() not in path.parents:
        return f"{rec['file']}: path leaves the session directory"
    if not path.is_file():
        return f"{rec['file']}: missing"
    actual = file_record(root, rec["file"])
    if actual["bytes"] != rec["bytes"] or actual["sha256"] != rec["sha256"]:
        return f"{rec['file']}: size or checksum mismatch"
    return None


class Session:
    def __init__(self, root: Path, info: dict):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        if (root / MANIFEST).exists():
            raise FileExistsError(f"{root} already holds a session")
        self.manifest = {"format_version": FORMAT_VERSION, "status": "running",
                         "created_utc": utc_now(), "finished_utc": None, "error": None,
                         **info, "references": {}, "points": []}
        self._write()

    def record(self, rel: str) -> dict:
        return file_record(self.root, rel)

    def set_reference(self, name: str, ref: dict):
        self.manifest["references"][name] = ref
        self._write()

    def add_point(self, point: dict):
        self.manifest["points"].append(point)
        self._write()

    def finish(self, status: str, error: str | None = None):
        self.manifest.update(status=status, error=error, finished_utc=utc_now())
        self._write()

    def _write(self):
        tmp = self.root / (MANIFEST + ".tmp")
        with tmp.open("w") as f:
            json.dump(self.manifest, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.root / MANIFEST)


def load_verified(root: Path) -> tuple[dict, list[dict], list[str]]:
    """Read a session for re-analysis. Returns (manifest, points whose every raw file and every
    reference they depend on verifies, problems). A run whose status is still "running" was
    interrupted; its verified points are usable, nothing resumes automatically."""
    manifest = json.loads((root / MANIFEST).read_text())
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"Unsupported manifest version {manifest.get('format_version')}")
    problems = []
    bad_refs = set()
    for name, ref in manifest["references"].items():
        if (p := _check_record(root, ref)) is not None:
            problems.append(f"reference {name}: {p}")
            bad_refs.add(name)
    good = []
    for pt in manifest["points"]:
        point_problems = [p for key in ("hdx", "camera") if key in pt
                          if (p := _check_record(root, pt[key])) is not None]
        point_problems += [f"depends on invalid reference {r}" for r in pt.get("references", [])
                           if r in bad_refs]
        if point_problems:
            problems += [f"point {pt['index']}: {p}" for p in point_problems]
        else:
            good.append(pt)
    if manifest["status"] == "running":
        problems.append("run did not finish (interrupted); only verified points are listed")
    return manifest, good, problems
