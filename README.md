# sandbox

Install script and wrapper files for an Augmented Reality Sandbox based on the
UC Davis SARndbox 2.8 / Kinect 3.10 / Vrui 8.0 packages.

`sandbox-install-3.0.sh` builds the UC Davis software, then unpacks
`Sandbox-Install-Payload-3.0.tar.gz` into the home directory. The payload
contains desktop icons, per-application Vrui configuration and the helper
scripts. The UC Davis code itself is not modified.

## Fresh install

On a new Linux Mint (Cinnamon) machine, logged in as the `sandbox` user:

```
wget -O sandbox-install-3.0.sh https://github.com/ideafablabs/sandbox/raw/main/sandbox-install-3.0.sh
bash sandbox-install-3.0.sh
```

It builds Vrui, Kinect and SARndbox, asks you to plug in the camera for the
intrinsic calibration, installs the payload and turns off the screensaver and
display sleep.

## Updating an existing install

Run the same two commands again on the sandbox PC. The script detects what is
already there:

- Vrui, Kinect and SARndbox are skipped when their binaries exist
  (`--force-build` rebuilds them anyway).
- The camera calibration is kept when `IntrinsicParameters-*.dat` exists
  (`--recalibrate-camera` redoes it).
- Every file the payload will replace is copied to `~/sandbox-backup-<date>` first.
- The payload is unpacked, then **your `BoxLayout.txt` and `ProjectorMatrix.dat`
  are put back**, so the sandbox stays calibrated.
- Old icons that the wizard replaces (ExtractPlanes, Measure3D, CalibrateProjector)
  are removed and `python3-tk` is installed if missing.

To refresh only the icons, scripts and wizard without touching the builds or the
camera, use:

```
bash sandbox-install-3.0.sh --payload-only
```

Other options: `--skip-settings` leaves the Cinnamon settings alone and
`--local-payload FILE` installs a tarball you copied over by hand (useful when the
machine has no internet). `--help` lists them all. After an update, close the
running sandbox with Esc and start it again from the Sandbox icon.

Note for maintainers: the script downloads the payload from the `main` branch on
GitHub, so commit and push a rebuilt `Sandbox-Install-Payload-3.0.tar.gz` before
running an update on a sandbox PC.

## Desktop icons

| Icon | What it does |
| --- | --- |
| Sandbox | Starts the sandbox (`run-sandbox.sh`). Also started automatically at login. |
| Calibrate Sandbox | One-window calibration wizard, see below. |
| XBackground | Shows the projector alignment grid. |
| RestoreDefaults | Restores the factory `BoxLayout.txt` and `ProjectorMatrix.dat`. |

## Calibrate Sandbox

`bin/CalibrateSandbox.py` (started by `bin/CalibrateSandbox.sh`) wraps the three
UC Davis calibration steps into one window with three phases:

1. **Base plane** - RawKinectViewer, "Average Frames" then key `1` to drag a box over flat sand.
2. **Box corners** - RawKinectViewer, key `2` on the four corners (lower-left, lower-right, upper-left, upper-right).
3. **Projector** - CalibrateProjector with the calibration disk, key `1` per point, key `2` to re-capture the background.

Phases 1 and 2 share one RawKinectViewer window. The wizard:

- shows which phases are done and the current values, and lets you tick which phases to run;
- sends instructions into the tool window (Vrui `showMessage` on stdin) as each step is reached;
- reads the values the tools print, uses the last plane and the last four corner clicks, checks them
  (negative offset, corner order, distance from the plane) and offers redo or auto-order;
- backs up `BoxLayout.txt` and `ProjectorMatrix.dat` into `etc/SARndbox-2.8/backups/` before writing;
- detects the projector resolution with `xrandr` and lets you confirm it;
- stops a running sandbox before a phase (it holds the camera) and can relaunch it afterwards;
- has an "Edit values" screen to adjust the plane and corners by hand. If the sandbox is running the
  height colors follow the new plane at once (`heightMapPlane` on the control pipe); corner changes
  need a sandbox restart, which the screen offers.

The overview also has a **Color height** card. It moves the color bands (the
sea level) up or down in centimeters relative to the calibrated base plane
without touching the calibration. A running sandbox shows each change at once
(`heightMapPlane` on the control pipe); Save writes a `heightMapPlane` line into
`etc/SARndbox-2.8/SARndbox.cfg`, which SARndbox reads at startup. The offset is
re-applied automatically when the base plane is recalibrated or edited.

The wizard needs only Python 3.6 or later with Tk (Mint 19.3 ships 3.6).
Everything is logged to `etc/SARndbox-2.8/calibration.log`. Run
`python3 bin/CalibrateSandbox.py --check` to print the paths the wizard uses.

The old single-step scripts (`ExtractPlanes.sh`, `Measure3D.sh`, `CalibrateProjector.sh`)
remain in `bin/` as a fallback but no longer have desktop icons.

## Updating the payload

The repo folder `Sandbox-Install-Payload-3.0/src/SARndbox-3.0` is packed into the tarball as
`src/SARndbox-2.8` because the installed tree lives in `~/src/SARndbox-2.8`. Rebuild the tarball with:

```
./make-payload.sh
```
