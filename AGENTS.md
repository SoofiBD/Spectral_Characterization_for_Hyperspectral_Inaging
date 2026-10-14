# Project rules (for any coding agent)

Python/PySide6 app for hyperspectral imaging. The ZWO ASI camera is the PRIMARY device (Camera tab:
standalone live view and raw frames; HSI Scan: camera + Arduino monochromator). The Ocean HDX is an
OPTIONAL channel for testing/comparison (ScanCoordinator accepts hdx=None). See README.md for layout,
scan sequence and manifest format.

- `acquisition/coordinator.py` is the only owner of the monochromator during a scan. The C++ code
  (`hsi/lamda_hsi`) talks to the camera only; the legacy ImGui app there must never run alongside.
- Raw data (HDX `.npz`, camera `.npy`, darks) is never modified after it is recorded in
  `manifest.json`. Analysis/exports read it and write elsewhere.
- A camera dark is applied only when its settings, shape and exposure match the frames.
- One asi_camd process owns the camera. The Camera tab lends its client to a scan only after the
  live-preview thread has finished; never open a second camera process while the tab is connected.
- Do not move hardware or start exposures on real devices without the user's explicit approval.
- Tests: `.venv/bin/python tests/test_scan.py` and
  `QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_simulation.py`,
  `QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_gui_safety.py`,
  `QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_scan_preflight.py`,
  `QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_hdx_recording.py`,
  `QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_camera.py`,
  `.venv/bin/python tests/test_camd.py` (fake SDK only) and
  `.venv/bin/python tests/test_legacy_data.py`.

## Not done yet
- Lab validation of the real camera live view and camera + monochromator scans (no HDX), then with HDX.
- Lab validation with all three real devices (single wavelength first, then a short scan).
- Lab validation of the implemented camera dark/white reference workflow and temperature policy.
- Absolute radiometric calibration (relative HDX white normalization and camera ENVI white correction are implemented).
- Simultaneous HDX + camera exposure (only after timing is measured on hardware).
