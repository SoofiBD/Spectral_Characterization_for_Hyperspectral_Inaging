# Spectral Characterization Suite

A desktop application that drives a motorized monochromator (Arduino, serial) and an Ocean Insight HDX
spectrometer. It runs automated wavelength-accuracy and resolution scans, analyzes the results, and
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
2. **Spectrometer (HDX) tab:** click Connect and set the integration time. Use Acquire for a single
   spectrum or Start Live for a continuous one. A live line analysis shows the peak, FWHM and SNR.
3. **Characterization tab:** set start, stop, step, averages, integration time, settling time and an
   optional backward scan, then click Start. Each point runs `GOTO:<nm>`, waits for `OK:DONE`,
   acquires and averages N spectra, then analyzes them. Plots and the table update live, and you can
   abort at any time.
   * Each scan creates `data/run_<timestamp>/`. `summary.csv` contains derived results;
     `dark.npz` and one numbered `.npz` per scan point preserve the full HDX wavelength axis
     and every uncorrected exposure. `run.json` records acquisition settings and device identity.
     Point files are written immediately, so completed points survive an aborted scan.
   * **Generate PDF Report** writes `reports/spectral_report_<timestamp>.pdf`, with the plot PNGs in
     `reports/plots/`.
   * **Load CSV…** reloads earlier results so you can re-plot them or build a new report.

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
`dark.npz`; check `run.json` before interpreting intensities. Slit fields show `0 = unknown`.

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
tests/test_simulation.py      headless end-to-end test
```

All hardware I/O runs in worker threads, so the GUI never blocks. Both controllers are guarded by locks.

## Testing
```bash
QT_QPA_PLATFORM=offscreen python tests/test_simulation.py
```
This test checks every device command and error handling, live acquisition, a full 400–800 nm
forward/backward scan, the CSV export and the PDF report.
