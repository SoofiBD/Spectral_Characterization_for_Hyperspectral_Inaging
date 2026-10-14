# Test results - 14 October 2026

## Camera-first update (14 October 2026, Linux only)

Environment: Linux cloud sandbox, Python 3.13, PySide6 6.12, offscreen Qt; simulated camera process
(`sim_camd.py`), simulated Arduino/HDX and the test-only ASI SDK. **Not yet run on macOS and not with a
real camera, monochromator or HDX.** Private legacy files were not present there (skip); on the Mac
their 91 hashes were checked separately and match.

| Test | Result |
| --- | --- |
| `test_scan.py` (incl. camera-only coordinator, real-mode dark prompts with fakes) | Passed |
| `test_scan_preflight.py` (incl. camera-only position check before handover) | Passed |
| `test_hdx_recording.py` | Passed |
| `test_simulation.py` (HDX-only and HDX + camera) | Passed |
| `test_gui_safety.py` | Passed |
| `test_camera.py` (Camera tab, handover, camera + Arduino without HDX, abort, loss, close) | Passed |
| `test_camd.py` (incl. preview loop and stationary frame on the C++ daemon) | Passed |

## Latest local run (before the camera-first update)

Environment: macOS on Apple Silicon, project Python 3.12 environment. Qt tests used the offscreen
platform; acquisition used simulated or fake devices only. Tests ran in groups of up to three.

| Test | Scope | Result | Duration |
| --- | --- | --- | ---: |
| `test_scan_preflight.py` | Position confirmation, stale-position and dark-setting regression checks | Passed | 1.89 s |
| `test_hdx_recording.py` | Raw/dark persistence, interruption and backward-point reopening | Passed | 7.14 s |
| `test_gui_safety.py` | Ownership, provenance, recovery and shutdown | Passed | 3.29 s |
| `test_simulation.py` | End-to-end simulation, CSV/PDF and camera export | Passed | 17.67 s |
| `test_scan.py` | Coordinator, cancellation, failures and recovery | Passed | 13.09 s |
| `test_camd.py` | C++ daemon using a test-only SDK | Passed | 4.80 s |
| `test_legacy_data.py` | Private historical measurement integrity | Passed, 91 NPZ files | 0.04 s |

The private legacy files are not distributed. That check reports a skip on a clean checkout without them.
The latest cleanup preserved all 141 local measurement files byte-for-byte. Detailed local evidence,
source hashes, logs and the PDF report are kept under `reports/validation_20261014/`, excluded from Git.
This document summarizes those records; it is not a live CI status badge.

## Hardware limits

A real Ocean HDX and monochromator were exercised at one wavelength before the latest fixes.
A full physical scan, cancellation/reconnect acceptance and revalidation of the final changes on
hardware have not yet been completed. Camera hardware was not tested in this session.
Measured line width is a property of the optical setup and is not an independent HDX resolution claim.

See [validation commands and limits](VALIDATION.md) for reproducible checks and required lab acceptance.
