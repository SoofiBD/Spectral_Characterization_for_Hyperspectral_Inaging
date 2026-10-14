# HSI - Spectral Characterization Suite

A Python desktop application for spectroscopy and hyperspectral imaging with an
Arduino-controlled monochromator, Ocean Insight HDX spectrometer and optional ZWO ASI camera.
Acquire traceable raw measurements, run wavelength scans, inspect spectral lines and export results.

## Features

- Stationary HDX measurements with configurable exposure count and integration time.
- Dark-reference acquisition, verified reference loading and dark-subtracted display.
- Single-wavelength and forward/backward scans with a preview of positions and exposure counts.
- Per-exposure raw files, timestamped session manifests and SHA-256 verification.
- Peak position, wavelength error, FWHM, repeatability and off-line-noise SNR analysis.
- CSV and PDF reports; optional monochrome RAW16 acquisition and ENVI cube export.
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

1. **Monochromator:** select the serial port, connect and compare the reported position with the physical counter.
2. **Spectrometer:** connect, set the integration time and acquire a dark reference with the input blocked.
3. **Stationary measurement:** restore illumination, choose the exposure count and click **Acquire**.
   The analysis target selects a peak-search window; it does not move the monochromator.
4. **Characterization:** choose start, stop, step and exposures per position. Equal start and stop values
   measure one wavelength. Confirm physical position before a real scan.
5. Use **Open Measurement**, **Open run / recover** or **Open Saved Folder** to inspect the saved data.

For example, 400-800 nm in 10 nm steps gives 41 forward positions. At five exposures per position,
that is 205 light exposures. Enabling the backward scan gives 81 positions and 405 light exposures;
the turnaround endpoint is measured once. Dark exposures are additional.

## Devices and prerequisites

| Component | Interface | Notes |
| --- | --- | --- |
| Monochromator | Arduino serial protocol | 9600 baud by default; position must match the physical counter. |
| Ocean HDX | Native USB driver or python-seabreeze | The native backend requires libusb; other Ocean models depend on the selected backend. |
| ZWO ASI camera | Optional C++17 `asi_camd` process | Physical camera support is built for macOS/Linux; simulation does not require the vendor binary. |

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
python tests/test_camd.py
python tests/test_legacy_data.py
```

Use the environment's Python on Windows and set `QT_QPA_PLATFORM=offscreen` through the shell's
environment-variable syntax. The C++ test needs a C++17 compiler and uses a fake SDK, not a camera.
The historical integrity test skips when private measurement files are absent.

The seven test groups passed locally on macOS on 14 October 2026. This is a dated local result,
not a claim that GitHub Actions has already run. Real-device testing has covered one HDX/monochromator
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
