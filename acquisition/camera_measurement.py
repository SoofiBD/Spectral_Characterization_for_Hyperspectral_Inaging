"""Camera RAW16 frames: validation shared by scans, and stationary frames recorded without a motor."""
from pathlib import Path

import numpy as np

from acquisition.session import Session, utc_now
from devices.asi_camera import CameraError


def check_raw16(path: Path, frame: dict, max_intensity) -> dict:
    """Verify a frame the camera process wrote; returns metadata to store (never changes the file)."""
    pixels = np.load(path, mmap_mode="r", allow_pickle=False)
    if pixels.dtype != np.dtype("uint16") or pixels.ndim != 2 or pixels.shape != (frame["height"], frame["width"]):
        raise CameraError("Camera driver did not return a valid RAW16 frame")
    saturated = int(np.count_nonzero(pixels >= max_intensity)) if max_intensity is not None else None
    return {"dtype": "uint16", "shape": list(pixels.shape), "saturated_pixels": saturated}


def record_camera_frame(camera, root: Path) -> Session:
    """Expose one RAW16 frame with the applied settings into a new session folder (no monochromator)."""
    if camera.settings is None:
        raise CameraError("Apply camera settings first")
    session = Session(root, {
        "kind": "camera_frame",
        "parameters": {"kind": "camera_frame", "simulation": camera.simulated, "camera": dict(camera.settings),
                       "target_source": "stationary frame; monochromator not used"},
        "devices": {"camera": {**camera.info, "simulated": camera.simulated}},
        "camera_settings": dict(camera.settings),
    })
    try:
        point = {"index": 0, "status": "partial", "direction": "stationary", "target_nm": None,
                 "reported_nm": None, "started_utc": utc_now(), "references": []}
        session.add_point(point)
        name = "frame_0000.npy"
        frame = camera.expose(root / name)
        meta = check_raw16(root / name, frame, camera.info.get("max_intensity"))
        point["camera"] = {**frame, **meta, **session.record(name),
                           "started_utc": point["started_utc"], "finished_utc": utc_now()}
        point.update(status="complete", finished_utc=utc_now())
        session.finish("complete")
        return session
    except BaseException as exc:
        session.finish("failed", str(exc) or type(exc).__name__)
        raise
