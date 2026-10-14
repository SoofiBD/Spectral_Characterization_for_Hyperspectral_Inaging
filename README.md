# HSI - Spectral Characterization Suite

A Python desktop application for hyperspectral imaging with a ZWO ASI monochrome camera (the
primary device) and an Arduino-controlled monochromator. An Ocean Insight HDX spectrometer is
optional, for testing, comparison and optimization. Acquire traceable raw frames, run wavelength
scans, inspect spectral lines and export results.

## Features

- Camera tab: standalone connection (real or simulated), live grayscale RAW16 view, exposure/gain/
  offset/binning/ROI and explicit raw-frame recording. Needs neither the HDX nor the Arduino.
- Camera + monochromator scans (single wavelength or ranges) with RAW16 frames, camera dark and ENVI
  export; the HDX can be added as an optional channel or used alone.
- Stationary HDX measurements with configurable exposure count and integration time.
- Dark-reference acquisition, verified reference loading and dark-subtracted display.
- Single-wavelength and forward/backward scans with a preview of positions and exposure counts.
- Per-exposure raw files, timestamped session manifests and SHA-256 verification.
- Peak position, wavelength error, FWHM, repeatability and off-line-noise SNR analysis.
- CSV reports; PDF reports for runs with HDX spectral metrics; ENVI cube export from camera runs.
- Hardware-free simulation, interrupted-run recovery and safeguards against overwriting raw data.

## Quick start

Requires **Python 3.11+**. From the repository directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py --simulate
```

On Windows, activate with `.venv\Scripts\activate` instead. Alternatively, use
`./run.sh --simulate` on macOS/Linux or `run.bat --simulate` on Windows; the launchers
create the environment and install dependencies on first use.

To use physical devices, start with `python main.py` and connect the required instruments.
The application does not connect to devices automatically on launch.

## Measurement workflow

1. **Camera:** choose *Real camera* (or *Simulated camera*), the camera index, **Connect**, set
   exposure/gain/offset/binning/ROI, **Apply Settings** and **Start Live**. Contrast scaling is display
   only. **Save Raw Frame** records the next RAW16 frame under `data/camera_frame_<time>/`.
2. **Monochromator:** select the serial port, connect and compare the reported position with the physical counter.
3. **HSI Scan:** the camera channel is selected by default; it uses the Camera tab settings. Choose
   start, stop and step (equal start and stop = one wavelength). Starting the scan stops the live view,
   waits for its last exposure and lends the same camera to the scan; after the scan the live view can be
   restarted. A real scan asks to confirm the physical position first; the dark step asks to block the light.
4. **HDX (optional):** tick *HDX spectrometer (optional)* in the scan to add spectra, or use the HDX tab
   alone for stationary measurements and HDX darks.
5. Use **Open run / recover**, **Open Measurement** or **Open Saved Folder** to inspect the saved data.

For example, 400-800 nm in 10 nm steps gives 41 positions and 41 camera frames; with the backward scan
81 positions (the turnaround endpoint is measured once). With the HDX at five exposures per position
that adds 205 (forward) or 405 HDX exposures. Dark frames/exposures are additional.

## Devices and prerequisites

| Component | Interface | Notes |
| --- | --- | --- |
| ZWO ASI camera (primary) | C++17 `asi_camd` process | Physical camera support is built for macOS/Linux; simulation does not require the vendor binary. |
| Monochromator | Arduino serial protocol | Needed for motorized scans only; 9600 baud by default; position must match the physical counter. |
| Ocean HDX (optional) | Native USB driver or python-seabreeze | The native backend requires libusb; other Ocean models depend on the selected backend. |

For HDX USB on macOS, install libusb with `brew install libusb`. Linux USB/serial permissions and
Windows backend setup are described in the [usage guide](docs/USAGE.md). To enable a physical camera,
restore the matching ZWO SDK library under `hsi/lamda_hsi/ASI_linux_mac_SDK_V1.41/lib/` and build `asi_camd`
as described in that guide. SDK libraries and local builds are excluded from Git.

## Data and exports

Measurements are written under `data/`, including a `manifest.json`, full wavelength axes,
individual raw exposures and applicable reference files. Derived reports are written under `reports/`.
These directories are local and excluded from version control. No private measurement data is required
for simulation or normal CI checks.

**Abort prevents subsequent work; it does not physically stop an active monochromator move.**
`SET` changes the declared position without motion; `HOME` and `RESET` move the instrument.
Never run the legacy ImGui controller alongside the Python application on the same devices.

## Tests and validation

```bash
python tests/test_scan.py
QT_QPA_PLATFORM=offscreen python tests/test_scan_preflight.py
QT_QPA_PLATFORM=offscreen python tests/test_hdx_recording.py
QT_QPA_PLATFORM=offscreen python tests/test_gui_safety.py
QT_QPA_PLATFORM=offscreen python tests/test_simulation.py
QT_QPA_PLATFORM=offscreen python tests/test_camera.py
python tests/test_camd.py
python tests/test_legacy_data.py
```

Use the environment's Python on Windows and set `QT_QPA_PLATFORM=offscreen` through the shell's
environment-variable syntax. The C++ test needs a C++17 compiler and uses a fake SDK, not a camera.
The historical integrity test skips when private measurement files are absent.

The seven earlier test groups passed locally on macOS on 14 October 2026; the camera-first changes
(including `test_camera.py`) were tested only on Linux with the simulated camera and fake SDK, not yet on
macOS or with a real camera. This is a dated local result, not a claim that GitHub Actions has already run. Real-device testing has covered one HDX/monochromator
wavelength; full scan acceptance and validation of the latest changes on hardware remain outstanding.
See the [test summary](docs/TEST_RESULTS.md) and [validation limits](docs/VALIDATION.md).

## Documentation

- [Detailed usage, file format, serial commands and camera build](docs/USAGE.md)
- [Supported drivers and extension contracts](docs/DRIVERS.md)
- [Validation, references, recovery and operational limits](docs/VALIDATION.md)
- [Dated test results](docs/TEST_RESULTS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [GitHub description, topics and publishing checklist](.github/REPOSITORY_SETTINGS.md)

The project has no project-wide license file yet. Third-party components retain their own notices.
