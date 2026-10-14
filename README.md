# Spectral Characterization Suite

A desktop application that drives a motorized monochromator (Arduino, serial), an Ocean Insight HDX
spectrometer and, optionally, a ZWO ASI camera (ASI533MM Pro) for hyperspectral imaging. It runs automated wavelength-accuracy and resolution scans, analyzes the results, and
writes CSV data and PDF reports. A built-in **simulation mode** lets you run everything without hardware.

## Installation

Requires Python 3.11 or newer.

```bash
cd spectral_characterization
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

If disk space is tight, `PySide6-Essentials` is enough to replace `PySide6`.

### Spectrometer drivers
* **Linux:** run `seabreeze_os_setup` once to install the udev rules.
* **Windows:** the device must use the libusb/WinUSB driver (`seabreeze_os_setup` also handles this),
  not the OceanView driver.
* **macOS:** no extra steps are needed.

## Quick start (one command)
* **Windows:** double-click `run.bat`, or run `run.bat --simulate` from a terminal.
* **macOS/Linux or Git Bash:** run `./run.sh` or `./run.sh --simulate`.

On the first run these scripts create `.venv` and install all dependencies. Later runs start the app
straight away. Do not copy the `.venv` folder to another computer; the scripts build it again there.

**Linux (one-time setup):**
```bash
sudo apt install python3-venv libxcb-cursor0 libusb-1.0-0   # Qt + libusb for pyseabreeze (Debian/Ubuntu)
sudo .venv/bin/seabreeze_os_setup                            # udev rules for the HDX, then replug it
sudo usermod -aG dialout $USER                               # serial port access for the Arduino, then log out/in
```

**Windows + HDX:** after the first install, run this command once so that the spectrometer uses the
libusb driver:
`.venv\Scripts\seabreeze_os_setup`

## Running

```bash
python main.py              # hardware mode
python main.py --simulate   # start with simulated devices
```

You can also switch **Simulation Mode** in the toolbar. The change takes effect on the next connect.
In the monochromator port list, the `SIMULATED` entry always picks the fake device.

## Workflow
1. **Monochromator tab:** pick the COM port and click Connect. From here you can send GOTO, HOME,
   RESET, POS and STATUS, type manual commands, and see the command history and the timestamped
   device traffic (`>>` sent, `<<` received, errors in red).
2. **Spectrometer (HDX) tab:** click Connect and set the integration time and exposure count.
   Acquire saves every raw exposure and the aggregate automatically under `data/hdx_measurement_<timestamp>/`.
   The analysis target does not move the monochromator; it only selects the peak-search window.
   Take Dark saves every dark exposure and its mean under `data/hdx_dark_<timestamp>/`.
   Open Measurement reloads verified data; Load Saved Dark restores a verified reference only for
   the same device, simulation/hardware mode, integration time and wavelength axis.
   Open Saved Folder opens the current recording directory. Save Spectrum exports the displayed
   spectrum as CSV; Start Live is a preview and does not automatically record frames.
   A live line analysis shows the peak, FWHM and SNR.
3. **Characterization tab:** set start, stop, step, averages, integration time, settling time and an
   optional backward scan, then click Start. Each point runs `GOTO:<nm>`, waits for `OK:DONE`,
   acquires and averages N spectra, then analyzes them. Plots and the table update live, and you can
   abort at any time. Abort prevents subsequent work; an active monochromator move is allowed to finish
   because the firmware has no supported motor-stop command.
   Equal start and stop values acquire one wavelength using the same raw-recording workflow.
   The parameter box shows the number of positions, HDX exposures and the first move (pre-positioning
   to start − 2 steps); the Output box shows the saved folder and has Open Saved Folder.
   With real hardware every Start asks the operator to confirm that the Arduino-reported position
   matches the physical counter (unknown position: use POS first; SET defines it without motion,
   HOME and RESET move). Nothing is created or moved on No; the confirmation is stored as
   `parameters.position_check` in `manifest.json`. Simulation skips this check.
   * Optional **Camera (monochrome RAW16)** box: each point also takes one camera exposure after
     the HDX spectra. The background step then also takes a camera dark frame.
   * Each scan creates `data/run_<timestamp>/`. `summary.csv` contains derived results;
     `dark.npz` and one numbered `.npz` per scan point preserve the full HDX wavelength axis
     and every uncorrected exposure; `camera_dark.npy` and one numbered `.npy` per point hold the
     untouched RAW16 camera frames. `manifest.json` (see below) records settings, devices,
     timestamps and the SHA-256 of every raw file. Every HDX exposure is written and recorded immediately, followed by the per-point aggregate
     and camera frame. Completed and partial points survive interruption; raw paths cannot be overwritten.
   * **Export ENVI Cube** writes `data/run_<timestamp>_envi/cube.hdr|raw|json` (float32 BSQ,
     forward points, camera dark subtracted) from the verified raw frames.
   * **Generate PDF Report** writes `reports/spectral_report_<timestamp>.pdf`, with the plot PNGs in
     `reports/plots/`.
   * **Load CSV…** reloads earlier results so you can re-plot them or build a new report.

## Scan sequence and session manifest
`acquisition/coordinator.py` is the only code that moves the monochromator during a scan.
Per point: `GOTO` → `OK:DONE:<pos>` within the move timeout → reported position must be within
0.5 nm of the target → settle → HDX raw exposures → camera RAW16 frame → files written and hashed →
`manifest.json` updated. Both device locks are held throughout the scan; pending manual/live work blocks startup. States: Preparing → Moving → Settling → Acquiring → Saving → Complete,
or Stopping → Aborted / Failed. Abort cancels a running camera exposure; points already recorded
are kept.

`manifest.json` (`format_version` 3; version 2 remains readable) is rewritten atomically after each exposure and point update. It holds `status`
(`running` while a scan is in progress; a `running` manifest that is left behind means the run was
interrupted), parameters, device identity (port, HDX model/serial, camera model/serial/SDK),
applied camera settings (exposure, gain, offset, bin, ROI, format, temperature), references
(`hdx_dark`, `camera_dark` with their settings) and one entry per point (target/reported
wavelength, UTC times, file names, sizes, SHA-256). `acquisition.session.load_verified(dir)`
re-checks every file and returns only the points that verify. Nothing resumes automatically.
Optional white-reference runs can be acquired separately and selected for camera ENVI correction or separate white-normalized spectrometer exports. See [validation and calibration](docs/VALIDATION.md).

## Camera process (asi_camd)
The ASI SDK runs in a separate C++ process, `hsi/lamda_hsi/build/asi_camd`, which writes frames
directly into the session folder. Build it once (macOS needs `brew install libusb pkg-config cmake`):
```bash
cmake -S hsi/lamda_hsi -B hsi/lamda_hsi/build -DLAMDA_HSI_BUILD_GUI=OFF
cmake --build hsi/lamda_hsi/build --target asi_camd
```
In simulation mode `devices/sim_camd.py` speaks the same protocol. The old ImGui app
(`hsi/lamda_hsi`, built with `LAMDA_HSI_BUILD_GUI=ON`) opens the monochromator serial port
itself: never run it while this application is connected.

## Serial protocol (9600 baud by default, selectable in the GUI; `\n` terminated)
| Command | Responses |
|---|---|
| `GOTO:<nm>` | `OK:MOVING` → `OK:DONE:<pos>` |
| `HOME` | `OK:HOMING` → `OK:DONE:<pos>` |
| `RESET:<nm>` | `OK:RESETTING` → `OK:RESET_DONE:<pos>` |
| `POS` | `OK:POS:<pos>` |
| `STATUS` | free text |
| any | `ERROR:<msg>` raises `MonochromatorError` |

## Analysis
* **Peak:** a Gaussian (log-parabolic) sub-pixel fit. The search is limited to ±30 nm around the set
  wavelength, after the baseline is subtracted.
* **Error:** measured − set. The summary reports the mean, the maximum |error|, RMSE and MAE.
* **FWHM:** taken from the interpolated half-maximum crossings.
* **Peak intensity / integrated intensity:** the integrated value uses the trapezoid rule over ±1.5 FWHM.
* **SNR:** peak above baseline divided by the robust (MAD) full-spectrum baseline noise;
  this is a peak-to-off-line-noise measure, not a shot-noise SNR at the peak.
* **Repeatability:** the mean and standard deviation of the peak position across the N individual scans
  at each point.
* **Forward/backward:** the difference between matched points estimates hysteresis. The
  turnaround endpoint is measured only once, since repeating it without a move would not
  constitute a backward approach.

CSV columns: `set_wavelength, measured_peak, error_nm, fwhm_nm, peak_intensity, integrated_intensity,
snr`, plus `direction, peak_mean_nm, peak_std_nm, reported_position, saturated_pixels`.

For independent analysis, open a point `.npz` with `numpy.load`; `raw_scans` has one row per
exposure, and `wavelength_nm` is the shared full axis. The matching `dark.npz` contains
`mean_raw` and its individual `raw_scans`. Subtract `mean_raw` from each light exposure before
averaging or applying a different peak/FWHM/SNR method. A run without a valid dark has no
`dark.npz`; check `manifest.json` (`references`) before interpreting intensities. Slit fields show `0 = unknown`.

## Project layout
```
main.py                       entry point
devices/monochromator.py      MonochromatorController + simulated serial device
devices/ocean_hdx.py          OceanHDXController (seabreeze) + simulated HDX
gui/main_window.py            main window, toolbar, event log, theme
gui/monochromator_panel.py    serial control UI
gui/hdx_panel.py              spectrometer UI + live plot
gui/characterization_panel.py automated scan worker, 6 result plots, export
gui/workers.py                thread-pool / QThread helpers
analysis/spectral_analysis.py line analysis + statistics
analysis/report_generator.py  ReportLab PDF
analysis/cube_export.py       ENVI cube from verified RAW16 frames
acquisition/coordinator.py    scan state machine (single monochromator owner)
acquisition/session.py        session folder, manifest, checksum verification
devices/asi_camera.py         asi_camd client (process, pipe protocol, abort)
devices/sim_camd.py           simulated camera process
hsi/lamda_hsi/src/asi_camd.cpp  C++ camera process (ASI SDK)
hsi/lamda_hsi/                legacy C++ HSI app (ImGui), kept as a fallback
tests/test_simulation.py      headless end-to-end test
tests/test_scan.py            coordinator: timeout, position, abort, recovery, calibration
```

Scan hardware I/O runs in a worker thread. Both controllers are guarded by locks held for the entire scan.
Shutdown waits asynchronously for active work rather than destroying a running worker. Mixed simulated/real
devices are rejected before camera initialization.

## Testing
```bash
QT_QPA_PLATFORM=offscreen python tests/test_simulation.py
```
This test checks every device command and error handling, live acquisition, a full 400–800 nm
forward/backward scan, the CSV export, the PDF report, and a short camera scan with ENVI export.
`python tests/test_scan.py` covers move timeout, position mismatch, abort during an exposure,
interrupted/corrupted session recovery, mismatched camera dark and manifest path escape.


## Device selection, recovery and validation

The Spectrometer tab selects native HDX or either seabreeze backend and an optional serial number.
Non-HDX Ocean devices use the integration limits and saturation level reported by their driver.
The camera panel selects index, exposure/gain/offset/binning and ROI; SDK validation remains authoritative.
Other manufacturers can supply a local driver without changing acquisition logic. See
[driver contracts and supported devices](docs/DRIVERS.md). An unknown USB device still needs a driver.

**Open run / recover** reloads a verified manifest and reanalyzes its complete points. Partial points,
missing references, checksum errors and unrecorded files are reported; originals are never rewritten.
**Load CSV** uses adjacent manifest/legacy run metadata, or reports unrecorded settings as unknown.
It does not substitute current device identities or GUI settings into historical reports.

For dark/white correction, temperature limits, all test commands, and the remaining physical-device
acceptance work, read [validation and operational limits](docs/VALIDATION.md). The software tests cannot
replace lab validation. The October 7 measurements are protected by a versioned review-baseline checksum
inventory; no claim is made about an unavailable pre-review backup.

## Saved validation results (14 October 2026)

`reports/validation_20261014/HSI_Test_ve_Dogrulama_2026-10-14.pdf` combines the actual HDX measurements,
the final local software-test results, cleanup details and remaining hardware-validation limits.
The same directory contains all seven test logs, JSON results, source/measurement checksums,
the measurement plot and Claude's review/fix records. Final software tests used simulated or fake devices only.

Completed patch/backup outputs and generated caches were moved outside the project into the Desktop
`HSI_arsiv_20261014_113118/` archive; `cleanup_manifest.json` records the original paths and SHA-256 values.
Source code, tests, SDKs, the virtual environment and all measurement data were retained.
