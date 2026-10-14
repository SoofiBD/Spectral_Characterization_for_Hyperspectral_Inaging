# Validation and operational limits

## Automated checks (no hardware)

```sh
.venv/bin/python tests/test_scan.py
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_scan_preflight.py
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_hdx_recording.py
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_simulation.py
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_gui_safety.py
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_camera.py
.venv/bin/python tests/test_camd.py
.venv/bin/python tests/test_legacy_data.py
```

`test_camd.py` compiles the production C++ daemon and camera wrapper with a **test-only SDK**.
It never links the vendor binary or accesses USB. It checks exact uint16 pixels, no overwrite,
SDK stop on cancellation and forced termination of a stuck SDK call. A C++17 compiler is needed.
The GUI tests use temporary folders and simulated devices only. `qt_bootstrap.py` works around
macOS Finder-hidden Qt plugins by copying plugin bytes to a process-local temporary directory.

The scan tests include actual child-process interruption, corrupt/missing references, partial
HDX data, settle cancellation, exclusive controller access, temperature mismatch, white reference
identity/settings/wavelength matching, and a spectrometer with a different pixel count/range.
`test_camera.py` covers the camera-first workflow with the simulated camera process: standalone
live view and raw-frame recording, settings changes during live view, non-blocking stop, preview/scan
camera handover (no overlapping commands), camera + simulated Arduino scans without any HDX connection
(one point and 500-520 nm), HDX + camera together, ENVI export and reopening, abort, simulated device
loss and closing during a long exposure.
These do not prove mechanical timing, USB stability or optical calibration on physical devices.

## Historical data

`tests/legacy_npz_sha256.json` records the 91 existing NPZ files at the October 12 review baseline.
Their aggregate and individual hashes were unchanged during this implementation. This is a
baseline for future integrity checks, **not proof of identity with an unavailable October 7 backup**.
The files remain private and outside Git. The integrity test skips when the private runs are absent.
Do not regenerate the baseline to make a failed integrity test pass.

## Storage and recovery

Use a local filesystem supporting atomic rename and hard links (APFS, ext4, NTFS). Network mounts,
FAT/exFAT and sudden power loss have not been validated. Raw publication fails rather than replacing
an existing file. Version 3 manifests retain per-exposure records and partial points; version 2
manifests remain readable. After a crash, open the run using **Open run / recover**. Complete verified
points are reanalyzed; partial points and unrecorded files are listed separately and retained.
No motor movement, automatic resume, raw rewrite or automatic trust of orphan files occurs.
CSV/PDF/ENVI exports are derived artifacts. Keep them outside the source run when recovering data.

## Dark and white references

Take dark frames with **all optical inputs physically blocked**. ASI533 has no mechanical shutter.
The camera dark uses the actual ROI/exposure/gain/offset/bin/format. Export rejects mismatched
settings, array shape, exposure or temperature difference above the selected limit. The default
2 °C tolerance is an editable software policy, not a validated sensor noise specification.
If temperature telemetry is unavailable for either frame, temperature matching cannot be checked;
null telemetry remains in the manifest. Cooling control/stability is not automated.

For camera white correction, acquire a separate scan with **This scan measures a white reference**
selected, dark measurement enabled, and a uniform white standard in the same measurement geometry.
Select that run for the sample's ENVI export. Both runs must have matching camera identity, settings,
direction and wavelength coverage. The output is `(sample - sample_dark)/(white - white_dark)`.
Non-positive white denominators become NaN and are counted; saturated white frames are rejected.
This is relative reflectance assuming a unit white standard, not absolute radiometric calibration.
HDX raw spectra remain in original counts. **Export white-normalized spectra** separately writes
`relative_signal` NPZ arrays using the same dark/white ratio. Spectrometer identity, wavelength axis,
integration time, scan direction, reference coverage and saturation are checked. Non-positive white
denominators become NaN; off-line pixels can therefore be undefined. These ratios are not absolute
radiometric calibration.

## Required lab acceptance (operator approval required)

1. Confirm the selected device identities, slit settings, optical geometry and reference temperature
   tolerance. Use fixed exposures and disable any independent application controlling these devices.
2. Validate one wavelength: compare firmware position with an independent spectral measurement,
   measure actual move/settle and HDX-to-camera timing, inspect uncorrected pixels and saturation.
3. Validate dark/white behavior and repeatability, including a change of ROI/exposure and temperature.
4. Run a short forward/backward scan; test cancellation and reconnect without assuming motor stop.
5. Repeat these checks for each new physical device/driver before scientific measurements.

The legacy ImGui application is archived and excluded from the default build. Its serial driver now
shares the advisory exclusive lock with Python, but unrelated third-party applications may ignore
that lock. Simultaneous HDX/camera exposure remains intentionally disabled until hardware timing is
measured. Do not declare the physical system validated from simulation results alone.
