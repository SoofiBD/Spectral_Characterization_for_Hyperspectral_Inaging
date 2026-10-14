# Device support and driver contracts

The acquisition coordinator is independent of the camera vendor and spectrometer pixel count.
A driver is still required for each manufacturer's SDK/protocol. Plugging in an unknown USB device
cannot make it supported automatically. Real device compatibility must be validated in the lab.

## Included drivers

- **Monochromator:** the existing MonoSpec/50 Arduino serial protocol. Position validation checks
  the firmware's `DONE` value, not an independent optical encoder. Abort prevents subsequent
  acquisition/movement, but waits for the active move to finish; there is no supported motor STOP
  command. A timeout requires checking the physical position before reconnecting/restarting.
- **Camera:** ZWO ASI SDK cameras supporting single-exposure RAW16, on macOS/Linux. Select the
  camera index and ROI in Characterization. Exposure, gain, offset, bin and ROI are applied through
  the SDK and the applied values are recorded. Unsupported settings fail before scanning.
  The current quantitative pipeline expects monochrome 2D uint16 data; it does not demosaic Bayer
  data or treat an RGB webcam stream as a calibrated monochrome measurement.
- **Spectrometer:** `native` is for HDX only. Select `pyseabreeze` or `cseabreeze` for other
  Ocean models supported by the installed python-seabreeze backend. Integration limits and
  saturation level come from the connected device, and arrays retain their native pixel count.
  Enter a serial number to select a specific instrument; empty selects the first available.
  Switching between seabreeze backends requires restarting the app.
- **Simulation:** no physical device access. Camera light frames depend on wavelength/exposure and
  the simulated white/sample target; dark frames contain bias/noise only.

## Adding another manufacturer

Install a trusted local Python driver and select its factory on startup:

```sh
python main.py --camera-driver my_lab.camera:CameraClient \
               --spectrometer-driver my_lab.spectrometer:SpectrometerController
```

These options load executable Python code. They are never read from a measurement manifest.
The factories and contracts are in `devices/contracts.py`. No change to the coordinator is needed.
The default factories are `devices.asi_camera:ASICameraClient` and
`devices.ocean_hdx:OceanHDXController`.

### Camera factory

Signature: `factory(simulate=False, log_path=None, index=0)`. Construction connects/selects a device
but must not expose or move hardware. `info` must describe `model`, `serial`, SDK/driver version,
sensor size, bit depth, and `max_intensity` (saturation in the actual stored RAW16 scale).
The ASI driver reports the ADC maximum shifted to the SDK RAW16 MSB-aligned scale; other drivers
must report their own encoding explicitly. Report `simulated` accurately; real/simulated mixtures are rejected.

- `configure(exposure_us, gain, offset, binning, roi)` returns applied settings. Keys:
  `exposure_us`, `gain`, `offset`, `bin`, `roi=[x,y,width,height]`, `format="RAW16"`.
  Reject unsupported settings; do not silently substitute preview or corrected frames.
- `expose(path)` blocks until a single exposure is saved as an untouched 2D uint16 NPY file.
  Use exclusive, durable publication; never replace an existing path. Return `width`, `height`,
  `exposure_us`, `temperature_c` (or null), `settings`, and any diagnostic metadata.
- `abort()` must be callable from another thread and bounded. The default ASI client first asks the
  C++ process to stop the exposure, then kills a stuck process after three seconds. This fallback
  bounds software waiting; it cannot prove that an unresponsive physical device has stopped.
- `close()` releases resources. Failures use `devices.asi_camera.CameraError`.
- `is_running` is False once the device session has ended (closed, aborted, crashed or unplugged).
  The Camera tab uses one client for live preview (repeated `expose()` into a temporary file that is
  deleted after loading) and lends that same client to a scan, so the factory must allow repeated
  `configure()`/`expose()` calls on one client.
- Optional `set_scene(wavelength_nm, dark, white)` supports coordinated simulation only.

A manufacturer without gain/offset/binning controls should accept only explicit neutral values
for those settings and reject other requests. Adding RGB/Bayer processing needs a separate
calibration design, not a silent conversion in this interface.

### Spectrometer factory

Signature: `factory(sim_state, backend)`. The controller must implement the full `Spectrometer`
protocol in `devices/contracts.py`, including the manual panel methods and dark state.
`available_backends` may provide the GUI choices; `backend` records the selected driver.

- All hardware operations and dark mutations use a `threading.RLock` named `_lock`.
  The coordinator holds the monochromator and spectrometer locks for the entire scan and refuses
  to start if either is busy. Do not dispatch hardware calls to an unrelated thread under this lock.
- `acquire_raw()` returns `(wavelength_nm, counts)` as same-length 1D NumPy arrays, with no dark,
  nonlinear, flat or baseline correction. The wavelength axis must remain fixed within a run.
- After connecting, publish `MIN_INT_MS`, `MAX_INT_MS`, `max_intensity`, `device_info`,
  `is_connected`, `is_simulated`, and `wavelengths`.
- `set_integration_time(ms)` validates the device limits and updates the stored applied value only
  after a successful SDK call. Bound hardware read timeouts; one active read may finish after abort.
- Dark state includes individual scans, mean, integration time and UTC acquisition time. Clear it
  on reconnect. A different device's dark must never be reused.

Before lab use, run the common scan tests against your driver with a fake SDK/transport, including
pixel counts different from 2068, exposure failure, cancellation, reconnect and partial recovery.
