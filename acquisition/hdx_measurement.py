"""Record stationary HDX measurements; this module never controls a motor."""
from pathlib import Path
import numpy as np

from acquisition.session import Session, save_spectra, utc_now
from devices.contracts import scan_access


def record_measurement(hdx, root: Path, integration_ms: float, count: int,
                       target_nm: float, dark: bool = False):
    if not isinstance(count, int) or count < 1 or not np.isfinite(target_nm):
        raise ValueError("Invalid exposure count or analysis wavelength")
    with scan_access(hdx):
        if not hdx.is_connected:
            raise ValueError("Connect the spectrometer first")
        hdx.set_integration_time(integration_ms)
        wl = hdx.wavelengths
        if wl.ndim != 1 or wl.size < 2 or not np.all(np.isfinite(wl)) or not np.all(np.diff(wl) > 0):
            raise ValueError("Invalid wavelength calibration")
        session = Session(root, {
            "kind": "hdx_dark" if dark else "stationary_hdx",
            "parameters": {"kind": "dark" if dark else "sample", "start": target_nm,
                           "stop": target_nm, "step": 10, "backward": False,
                           "integration_ms": hdx.integration_time_ms, "averages": count,
                           "dark_averages": count if dark else len(hdx.dark_scans) if hdx.dark_active else 0,
                           "settle_s": 0, "measure_dark": dark,
                           "simulation": hdx.is_simulated, "camera": None,
                           "target_source": "operator-entered analysis target; no motor motion",
                           "dark": "measured" if dark else "existing dark" if hdx.dark_active else "none"},
            "devices": {"spectrometer": {"info": hdx.device_info, "backend": hdx.backend,
                                         "simulated": hdx.is_simulated,
                                         "max_intensity": hdx.max_intensity}},
            "dark_exposures": [],
        })
        try:
            if not dark and hdx.dark_active:
                save_spectra(root / "dark.npz", wavelength_nm=wl,
                             raw_scans=np.asarray(hdx.dark_scans), mean_raw=hdx.dark)
                session.set_reference("hdx_dark", {
                    **session.record("dark.npz"), "integration_ms": hdx.dark_integration_ms,
                    "captured_utc": hdx.dark_captured_utc, "n_scans": len(hdx.dark_scans),
                    "measured_in_this_session": False})
            point = {"index": 0, "status": "partial", "direction": "forward",
                     "target_nm": target_nm, "reported_nm": None,
                     "started_utc": utc_now(), "references": list(session.manifest["references"]),
                     "hdx_exposures": []}
            if not dark:
                session.add_point(point)
            scans = []
            for index in range(count):
                started = utc_now()
                axis, counts = hdx.acquire_raw()
                if not np.array_equal(axis, wl) or counts.shape != wl.shape or not np.all(np.isfinite(counts)):
                    raise ValueError("Spectrometer returned invalid axis or intensity values")
                name = f"{'dark' if dark else '0000_forward'}_exposure{index:04d}.npz"
                save_spectra(root / name, wavelength_nm=wl, raw_scans=counts[None, :],
                             integration_ms=hdx.integration_time_ms, captured_at_utc=started)
                record = {**session.record(name), "started_utc": started, "finished_utc": utc_now()}
                if dark:
                    session.manifest["dark_exposures"].append(record)
                else:
                    point["hdx_exposures"].append(record)
                session.checkpoint()
                scans.append(counts)
            raw = np.asarray(scans)
            mean = raw.mean(axis=0)
            if dark:
                save_spectra(root / "dark.npz", wavelength_nm=wl, raw_scans=raw, mean_raw=mean)
                captured = utc_now()
                session.set_reference("hdx_dark", {
                    **session.record("dark.npz"), "integration_ms": hdx.integration_time_ms,
                    "captured_utc": captured, "n_scans": count, "measured_in_this_session": True})
            else:
                name = "0000_forward_spectrum.npz"
                save_spectra(root / name, wavelength_nm=wl, raw_scans=raw,
                             integration_ms=hdx.integration_time_ms, set_wavelength_nm=target_nm)
                point.update(status="complete", finished_utc=utc_now(), hdx={
                    **session.record(name), "integration_ms": hdx.integration_time_ms, "n_scans": count})
                session.checkpoint()
            session.finish("complete")
            if dark:
                hdx.dark_scans, hdx.dark = scans, mean.copy()
                hdx.dark_integration_ms, hdx.dark_captured_utc = hdx.integration_time_ms, captured
            elif hdx.dark_active:
                mean = mean - hdx.dark
            saturated = int(np.count_nonzero(raw >= hdx.max_intensity))
            return session, wl, mean, saturated
        except Exception as exc:
            session.finish("failed", str(exc))
            raise
