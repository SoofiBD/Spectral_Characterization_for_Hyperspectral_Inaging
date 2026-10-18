# Detailed usage guide

The application records hyperspectral scans with a ZWO ASI camera and an Arduino-controlled
monochromator. The Ocean Insight HDX is an optional comparison channel. Use simulation mode
to try acquisition, analysis and exports without hardware.

## Installation

Requires Python 3.11 or newer.

```bash
cd spectral_characterization
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` installs `PySide6-Essentials` only (QtCore, QtGui, QtWidgets, QtSvg); the full `PySide6` (~1 GB more) is not needed.

### Spectrometer drivers
* **Linux:** run `seabreeze_os_setup` once to install the udev rules.
* **Windows:** the device must use the libusb/WinUSB driver (`seabreeze_os_setup` also handles this),
  not the OceanView driver.
* **macOS:** install the libusb runtime for the native USB backend (`brew install libusb`).

## Launchers
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

You can also switch **Sim** (Simulation Mode) at the bottom of the navigation rail. The change takes
effect on the next connect.

The left navigation rail opens the Camera, Monochromator (Mono), HSI Scan (Scan) and HDX pages. The dot on
each icon and the badge in the page header show the device state (grey: not connected, amber: simulated,
green: real hardware); during a scan the Scan badge shows progress. **Log** shows or hides the event log
and **Help** lists the shortcuts. Spin-box values, channel choices, window layout and the last tab are stored with
`QSettings` and restored at the next start. The white-reference flag, SET/RESET values and
dark subtraction must be set again for each session.

| Shortcut | Action |
| --- | --- |
| Ctrl+1 … Ctrl+4 | Camera / Monochromator / HSI Scan / HDX page |
| F5 | Camera: start / stop live view |
| Ctrl+S | Camera: save the next raw frame |
| Ctrl+Return | HSI Scan: start the scan (same checks and confirmations as the button) |
| Ctrl+O | Open a recorded run |
| Ctrl+L | Show / hide the event log |
| F1 | Shortcut list |

During live view, exposure, gain, offset and binning changes are applied before the next frame without clicking **Apply
Settings**. The HSI Scan tab shows a readiness checklist, the camera settings the scan will use, the last
recorded camera frame (decimated display copy; the raw file is only read), mean camera counts per
wavelength (*Camera Signal* tab) and an estimated remaining time. Scan parameters are locked while a scan runs.
In the monochromator port list, the `SIMULATED` entry always picks the fake device.

## Workflow
The camera is the primary device and the HDX is optional. Tabs: **Camera**, **Monochromator**,
**HSI Scan**, **HDX (optional)**.

1. **Camera tab** (works alone; HDX and Arduino are not needed): choose *Real camera (asi_camd)* or
   *Simulated camera* (follows Simulation Mode while disconnected) and the camera index, click
   **Connect** (identity: model, serial, SDK, sensor size, bit depth, pixel size). Set exposure, gain,
   offset, binning and ROI and click **Apply Settings**; the label shows the values the camera SDK
   actually applied. **Start Live** shows a grayscale preview without keeping the frames.
   Auto contrast and levels affect only the display; RAW16 data is unchanged.
   **Stop Live** returns immediately; the current exposure finishes in the background (closing the
   window aborts it). **Save Raw Frame** is the only way the tab writes data: the next RAW16 frame goes
   to `data/camera_frame_<timestamp>/` (`frame_0000.npy` + `manifest.json` with device identity,
   applied settings, UTC times, shape, dtype, size and SHA-256). Disconnect removes the preview folder.
   If the camera process ends (USB unplugged, driver crash, abort), the tab releases it and asks to reconnect.
2. **Monochromator tab:** pick the COM port and click Connect. From here you can send GOTO, HOME,
   RESET, SET, POS and STATUS, type manual commands, and see the command history and the timestamped
   device traffic (`>>` sent, `<<` received, errors in red).
3. **HDX (optional) tab:** click Connect and set the integration time and exposure count.
   Acquire saves every raw exposure and the aggregate automatically under `data/hdx_measurement_<timestamp>/`.
   The analysis target does not move the monochromator; it only selects the peak-search window.
   Take Dark saves every dark exposure and its mean under `data/hdx_dark_<timestamp>/`.
   Open Measurement reloads verified data; Load Saved Dark restores a verified reference only for
   the same device, simulation/hardware mode, integration time and wavelength axis.
   Open Folder opens the current recording directory. Save Spectrum exports the displayed
   spectrum as CSV; Start Live is a preview and does not automatically record frames.
   A live line analysis shows the peak, FWHM and SNR.
4. **HSI Scan tab:** select the channels: **Camera — primary** (default; uses the Camera tab
   settings) and/or **HDX spectrometer (optional)** (averages, integration, HDX dark averages). The
   monochromator must be connected (the scan moves it); the HDX only when its box is ticked. Set start,
   stop, step, settling time and an optional backward scan, then click Start. If the Camera tab is
   connected, Start stops its live view, the scan waits until the last preview exposure has finished and
   then uses the same camera process (one owner at a time); otherwise the scan opens its own camera
   process and closes it afterwards. Each point runs `GOTO:<nm>`, waits for `OK:DONE`, takes the
   optional HDX spectra and then one camera frame. Without the HDX no spectra, peaks, FWHM or SNR are
   produced: the table shows the camera mean counts and saturated pixels, and the PDF report and
   white-normalized spectra (HDX features) are disabled; ENVI export and Open run work. Plots and the table update live, and you can
   abort at any time. Abort prevents subsequent work; an active monochromator move is allowed to finish
   because the firmware has no supported motor-stop command.
   Equal start and stop values acquire one wavelength using the same raw-recording workflow.
   The parameter box shows the number of positions, HDX exposures and the first move (pre-positioning
   to start − 2 steps); the Results card shows the saved folder and has Open Folder.
   With real hardware every Start asks the operator to confirm that the Arduino-reported position
   matches the physical counter (unknown position: use POS first; SET defines it without motion,
   HOME and RESET move). Nothing is created or moved on No; the confirmation is stored as
   `parameters.position_check` in `manifest.json`. Simulation skips this check.
   * **Measure background (dark) first** asks once (real hardware) to block the light, then takes one
     camera dark frame and, only if the HDX is selected, the HDX dark exposures. They are separate
     references (`camera_dark`, `hdx_dark`); no HDX dark is requested or recorded without the HDX.
   * Each scan creates `data/run_<timestamp>/`. `summary.csv` contains derived results;
     `dark.npz` and one numbered `.npz` per scan point preserve the full HDX wavelength axis
     and every uncorrected exposure; `camera_dark.npy` and one numbered `.npy` per point hold the
     untouched RAW16 camera frames. `manifest.json` (see below) records settings, devices,
     timestamps and the SHA-256 of every raw file. Every HDX exposure is written and recorded immediately, followed by the per-point aggregate
     and camera frame. Completed and partial points survive interruption; raw paths cannot be overwritten.
   * **ENVI Cube** writes `data/run_<timestamp>_envi/cube.hdr|raw|json` (float32 BSQ,
     forward points, camera dark subtracted) from the verified raw frames.
   * **Review ROI…** picks a run from a table of `data/` (status, kind, channels, range,
     camera dark; Browse… for other folders) and opens a read-only review: all frames on one common
     display scale, two regions A/B (in full-resolution pixels of the stored frame), their
     dark-subtracted means ± standard error and A/B versus set wavelength for forward and backward
     points. Saturated pixels inside a region, a denominator below 3 standard errors and an unknown
     saturation level are flagged; forward/backward differences are matched by set wavelength.
     If a white-reference run is selected (same rules as the ENVI export: complete `kind = white`, same
     camera, settings and wavelengths), the same regions are divided by the white run's regions
     ("White-corrected" mode). **Save ROI preset…** / **Load ROI…** reuse regions on runs with the same
     frame geometry (an exported `roi.json` also works). **Export analysis…** writes
     `data/run_<timestamp>_roi_<stamp>/` (`roi_signals.csv`, `roi_forward_backward.csv`, `roi.json`
     with manifest/frame checksums, dark, settings and method, plus PNGs); the folder appears only
     when complete. The standard error is pixel scatter within one frame, not repeat-measurement
     noise.
   * **PDF Report** writes `reports/spectral_report_<timestamp>.pdf`, with the plot PNGs in
     `reports/plots/`.
   * **Load CSV…** reloads earlier results so you can re-plot them or build a new report.

## Scan sequence and session manifest
`acquisition/coordinator.py` is the only code that moves the monochromator during a scan.
Per point: `GOTO` → `OK:DONE:<pos>` within the move timeout → reported position must be within
0.5 nm of the target → settle → optional HDX raw exposures → camera RAW16 frame → files written and
hashed → `manifest.json` updated. `manifest.channels` lists the channels used (`camera`, `hdx`); the
spectrometer appears under `devices` only when it was used. The monochromator (and HDX) locks are held
throughout the scan; pending manual/live work blocks startup. States: Preparing → Moving → Settling → Acquiring → Saving → Complete,
or Stopping → Aborted / Failed. Abort cancels a running camera exposure; points already recorded
are kept.

`manifest.json` (`format_version` 3; version 2 remains readable) is rewritten atomically after each exposure and point update. It holds `status`
(`running` while a scan is in progress; a `running` manifest that is left behind means the run was
interrupted), parameters, device identity (port, HDX model/serial, camera model/serial/SDK),
applied camera settings (exposure, gain, offset, bin, ROI, format, temperature), references
(`hdx_dark`, `camera_dark` with their settings) and one entry per point (target/reported
wavelength, UTC times, file names, sizes, SHA-256). `acquisition.session.load_verified(dir)`
re-checks every file and returns only the points that verify. Nothing resumes automatically.
Optional white-reference runs can be acquired separately and selected for camera ENVI correction or separate white-normalized spectrometer exports. See [validation and calibration](VALIDATION.md).

## Camera process (asi_camd)
The ASI SDK runs in a separate C++ process, `hsi/lamda_hsi/build/asi_camd`, which writes frames
directly into the session folder (or the preview temp folder). One process is the only owner of the
camera: the Camera tab's live view and a scan never use it at the same time. Build it once (macOS needs `brew install libusb pkg-config cmake`):
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
| `RESET:<nm>` | Move back by `<nm>`, then declare that position zero; `OK:RESET_DONE:0` |
| `SET:<nm>` | Define the current position without motion; `OK:SET:<pos>` |
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
gui/camera_panel.py           camera UI: connect, live RAW16 view, settings, raw frame recording
gui/hdx_panel.py              optional HDX spectrometer UI + live plot
acquisition/camera_measurement.py RAW16 frame checks + stationary camera frame recording
acquisition/hdx_measurement.py stationary raw/dark recording
gui/characterization_panel.py automated scan worker, result plots, export
gui/workers.py                thread-pool / QThread helpers
analysis/spectral_analysis.py line analysis + statistics
analysis/report_generator.py  ReportLab PDF
analysis/cube_export.py       ENVI cube from verified RAW16 frames
analysis/roi_analysis.py      two-region camera analysis, presets, run list (Qt-free)
gui/camera_review.py          run picker + read-only camera ROI review dialog
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
See [validation commands and limits](VALIDATION.md) for all test commands.

```bash
QT_QPA_PLATFORM=offscreen python tests/test_simulation.py
QT_QPA_PLATFORM=offscreen python tests/test_camera.py
```
`test_simulation.py` checks simulated device commands, error handling, live acquisition, a 400–800 nm
forward/backward scan, the CSV export, the PDF report, and a short HDX + camera scan with ENVI export.
`tests/test_camera.py` covers the camera-first workflow (Camera tab alone, camera + Arduino scans
without HDX, preview/scan handover, abort, device loss and shutdown) with the simulated camera.
`tests/test_ui_workflow.py` covers remembered settings, the readiness checklist, scan progress text and
the camera scan views.
`python tests/test_scan.py` covers move timeout, position mismatch, abort during an exposure,
interrupted/corrupted session recovery, mismatched camera dark and manifest path escape.


## Device selection, recovery and validation

The HDX (optional) tab selects native HDX or either seabreeze backend and an optional serial number.
Non-HDX Ocean devices use the integration limits and saturation level reported by their driver.
The camera panel selects index, exposure/gain/offset/binning and ROI; SDK validation remains authoritative.
Other manufacturers can supply a local driver without changing acquisition logic. See
[driver contracts and supported devices](DRIVERS.md). An unknown USB device still needs a driver.

**Open Run…** reloads a verified manifest and reanalyzes its complete points. Partial points,
missing references, checksum errors and unrecorded files are reported; originals are never rewritten.
**Load CSV** uses adjacent manifest/legacy run metadata, or reports unrecorded settings as unknown.
It does not substitute current device identities or GUI settings into historical reports.

For dark/white correction, temperature limits, all test commands, and the remaining physical-device
acceptance work, read [validation and operational limits](VALIDATION.md). Historical measurement
checksums are stored in `tests/legacy_npz_sha256.json`; the measurements themselves stay outside Git.
