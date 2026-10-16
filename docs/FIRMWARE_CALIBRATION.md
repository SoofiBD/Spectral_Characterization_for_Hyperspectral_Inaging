# Monochromator wavelength scale: moving the fix into the Arduino firmware

## Background

Lab measurement on 2026-10-16 (mechanical wavelength counter vs firmware position, 0-800 nm,
after one `SET` at 397.6 nm):

| Firmware (nm) | Counter forward | Counter backward |
|---|---|---|
| 400 | 397.6 (SET here) | 400.0 |
| 500 | 498.8 | 498.8 |
| 600 | 597.6 | 597.6 |
| 700 | 696.6 | 696.4 |
| 800 | 795.2 | - |
| 0 | - | 4.7 |

Linear fit: `counter = 0.9882 * firmware + 4.70`, max residual 0.16 nm. Forward and backward agree
within 0.2 nm, so backlash is negligible. The grating turns only **0.9882x** the commanded
distance: the firmware's steps-per-nm constant is **1.19 % too small**.

Until the firmware is fixed, the Python program corrects this in software:
`NM_SCALE = 0.9882` in `devices/monochromator.py`. Every GOTO/SET/RESET argument is divided by
it and every reported position is multiplied by it (panel buttons, the manual command box and
scans). Simulation uses 1.0. Each scan manifest records the value under
`devices.monochromator.nm_scale`.

**Do the firmware fix and the software fix never at the same time**: that would correct twice
(about 1.2 % wrong in the other direction).

## What you need

- The firmware source (`.ino`) that is on the Arduino now. It cannot be read back from the board.
- The board type (Uno / Nano / Mega, printed on the board) and its USB port
  (`/dev/cu.usbmodem*` on macOS).
- Arduino IDE, or `arduino-cli` (`brew install arduino-cli`).
- Close the Python program before uploading (only one program can open the port).

## Steps

1. **Back up.** Copy the original `.ino` unchanged to `firmware/monochromator_original.ino`
   in this repository and commit it, so the current behaviour can always be restored.

2. **Find the scale constant.** Search the sketch for the value that converts nm into motor
   steps. Typical names: `STEPS_PER_NM`, `stepsPerNm`, `NM_PER_STEP`, `STEPS_PER_REV`,
   `GEAR_RATIO`, or a number in a line like `steps = nm * 40.0;`.

3. **Change it by 1.19 %** (exactly: 1 / 0.9882 = 1.01194):
   - If it is *steps per nm* (nm is multiplied by it): `new = old * 1.01194`
   - If it is *nm per step* (nm is divided by it): `new = old * 0.9882`
   - If it is built from several constants (steps/rev, microsteps, gear ratio...), change only
     one factor, the same way.
   - Keep it a float (`40.4776`, not `40`). If the code stores steps as `int`, make sure the
     multiplication happens in float before rounding.
   - Add a comment: `// x1.01194 after counter calibration 2026-10-16`.

4. **Compile and upload.**
   - Arduino IDE: select board and port, Upload.
   - `arduino-cli`:
     ```
     arduino-cli core install arduino:avr
     arduino-cli compile --fqbn arduino:avr:uno firmware/monochromator
     arduino-cli upload  --fqbn arduino:avr:uno -p /dev/cu.usbmodem1201 firmware/monochromator
     ```
     (use `arduino:avr:nano` or `arduino:avr:mega` for those boards.)

5. **Remove the software correction.** In `devices/monochromator.py` set
   ```python
   NM_SCALE = 1.0
   ```
   and update its comment to say the firmware now carries the calibration (date + new constant).
   Leave the conversion code in place; with 1.0 it does nothing. Run the tests
   (`.venv/bin/python tests/test_scan.py` and the others listed in `AGENTS.md`).

6. **Verify on hardware** (same test as on 2026-10-16):
   1. Connect, `GOTO` about 380, then `GOTO:400` (approach from below).
   2. Read the counter and `SET:<counter value>` once.
   3. `GOTO` 500, 600, 700, 800, then back 700, 600, 500, 400. Do not `SET` again.
   4. Write down the counter at every point. Pass: every point within +/-0.3 nm of the program.
   If the error is still proportional to wavelength, compute the new ratio
   `a = (counter_800 - counter_400) / (800 - 400)` and repeat step 3 with `old * (1 / a)`.

7. **Commit** the new `.ino` (e.g. `firmware/monochromator/monochromator.ino`), the
   `NM_SCALE = 1.0` change and the verification table.

## Notes

- Every Arduino reset (port opened, USB replugged, program restarted) sets the firmware position
  to 0. After each connect, read the counter and `SET` once before scanning. This is unrelated
  to the scale error and is not fixed by this change.
- Data recorded before this correction has a wavelength axis that is about 1.2 % off (about
  5 nm at 800 nm when zeroed near 400 nm). Raw data is never edited; take that into account in
  analysis or repeat the measurement.
