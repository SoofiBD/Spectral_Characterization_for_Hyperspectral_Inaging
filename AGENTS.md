# Project rules (for any coding agent)

Python/PySide6 app driving a monochromator, an Ocean HDX and a ZWO ASI camera. See README.md for
layout, scan sequence and manifest format.

- `acquisition/coordinator.py` is the only owner of the monochromator during a scan. The C++ code
  (`hsi/lamda_hsi`) talks to the camera only; the legacy ImGui app there must never run alongside.
- Raw data (HDX `.npz`, camera `.npy`, darks) is never modified after it is recorded in
  `manifest.json`. Analysis/exports read it and write elsewhere.
- A camera dark is applied only when its settings, shape and exposure match the frames.
- Do not move hardware or start exposures on real devices without the user's explicit approval.
- Tests: `.venv/bin/python tests/test_scan.py` and
  `QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_simulation.py`,
  `QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_gui_safety.py`,
  `.venv/bin/python tests/test_camd.py` (fake SDK only) and
  `.venv/bin/python tests/test_legacy_data.py`.

## Not done yet
- Lab validation with all three real devices (single wavelength first, then a short scan).
- Lab validation of the implemented camera dark/white reference workflow and temperature policy.
- Absolute radiometric calibration (relative HDX white normalization and camera ENVI white correction are implemented).
- Simultaneous HDX + camera exposure (only after timing is measured on hardware).
