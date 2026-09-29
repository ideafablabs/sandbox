# sandbox

Install script and wrapper files for an Augmented Reality Sandbox based on the
UC Davis SARndbox 2.8 / Kinect 3.10 / Vrui 8.0 packages.

`sandbox-install-3.0.sh` builds the UC Davis software, then unpacks
`Sandbox-Install-Payload-3.0.tar.gz` into the home directory. The payload
contains desktop icons, per-application Vrui configuration and the helper
scripts. The UC Davis code itself is not modified.

## Fresh install

On a new Linux Mint (Cinnamon) machine, logged in as the `sandbox` user, paste
this one line into a terminal:

```
wget -O sandbox-install-3.0.sh https://github.com/ideafablabs/sandbox/raw/main/sandbox-install-3.0.sh && chmod +x sandbox-install-3.0.sh && ./sandbox-install-3.0.sh
```

It downloads the install script, makes it executable and runs it. Run it as the
normal user, not with `sudo`: it asks for the password itself where it needs
root.

It builds Vrui, Kinect and SARndbox, asks you to plug in the camera for the
intrinsic calibration, installs the payload and sets the machine up to run
unattended:

- **The screen never goes dark.** Screensaver and screen lock off, no display
  sleep or dimming, and the computer never suspends on its own. X's own blanking
  and DPMS are turned off as well, by `bin/disable-screen-blanking.sh`, which
  runs from a login autostart entry and again at the start of `run-sandbox.sh`,
  because those come back every time the X server restarts.
- **The account logs in automatically.** The user that ran the installer is
  written into `/etc/lightdm/lightdm.conf` under `[Seat:*]`, so the machine comes
  back up into the desktop by itself after a power cut. The file as it was before
  the first run is kept as `lightdm.conf.sandbox-orig`.
- **The power button turns the computer off** when pressed, with no dialog.
- Sounds off, the `ifl-desktop-bg.png` background, and larger desktop icons with
  bigger labels.

The settings work on Cinnamon (and MATE or GNOME if that is what the machine
runs). Every setting is read back after it is written, and the summary at the end
says what the screensaver and power button actually read, names anything that did
not stick and anything the installed release does not have. If a setting is ever
lost, re-run the script and read that summary. Use `--icon-zoom largest` (or
`standard`, `large`, `larger`) to change the icon size, and `--skip-settings` to
leave all of the above alone.

## Updating an existing install

Paste the same line again on the sandbox PC. The script detects what is
already there:

- Vrui, Kinect and SARndbox are skipped when their binaries exist
  (`--force-build` rebuilds them anyway).
- The camera calibration is kept when `IntrinsicParameters-*.dat` exists
  (`--recalibrate-camera` redoes it).
- Every file the payload will replace is copied to `~/sandbox-backup-<date>` first.
- The payload is unpacked, then **your `BoxLayout.txt`, `ProjectorMatrix.dat` and
  `EdgeMask.cfg` are put back**, so the sandbox stays calibrated.
- Old icons that the wizard replaces (ExtractPlanes, Measure3D, CalibrateProjector)
  are removed and `python3-tk` is installed if missing.

To refresh only the icons, scripts and wizard without touching the builds or the
camera, add `--payload-only`, which is the usual way to pick up a change to the
wizard:

```
wget -O sandbox-install-3.0.sh https://github.com/ideafablabs/sandbox/raw/main/sandbox-install-3.0.sh && chmod +x sandbox-install-3.0.sh && ./sandbox-install-3.0.sh --payload-only
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

The XBackground and RestoreDefaults icons are gone: the wizard has the alignment
grid on its projector screen and a **Restore factory defaults** button on the
overview. An update removes the two old icons from the desktop. Nothing else is
lost: `bin/Restore.sh` is still there and XBackground is still on the path, so
both can be run from a terminal.

## Calibrate Sandbox

`bin/CalibrateSandbox.py` (started by `bin/CalibrateSandbox.sh`) wraps the
UC Davis calibration steps into one window with four phases:

1. **Base plane** - RawKinectViewer, "Average Frames" (pressed by the SandboxHelper plugin, see below) then key `1` to drag a box over flat sand.
2. **Box corners** - RawKinectViewer, key `2` on the four corners (lower-left, lower-right, upper-left, upper-right).
3. **Projector** - CalibrateProjector with the calibration disk, key `1` per point, key `2` to re-capture the background.
4. **Edge mask** - the SandboxMask plugin in the running sandbox; nudge each edge from the wizard until the black just covers the box wall.

A fifth phase, the camera depth lens, is built but hidden (see below). Phases 1
and 2 share one RawKinectViewer window. The wizard:

- shows which phases are done and the current values, and lets you tick which phases to run;
- sends instructions into the tool window (Vrui `showMessage` on stdin) as each step is reached.
  In phase 2 every corner press gets a popup that confirms the corner, shows its position and
  names the next corner; the fourth one also flags a suspicious set (wrong order, duplicate,
  far from the base plane). No popup after pressing `2` means the camera has no depth reading
  at that pixel (black in the depth image), so the tool printed nothing: move further onto
  the sand and press again. "Send instructions again" repeats the hint for the current step;
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
without touching the calibration. There is no Save button: every change is
written straight into `etc/SARndbox-2.8/SARndbox.cfg` as a `heightMapPlane` line
(once the slider settles, and `SARndbox.cfg` is backed up once per visit), and a
running sandbox shows it at once through `heightMapPlane` on the control pipe.
The offset is
re-applied automatically when the base plane is recalibrated or edited, and
**Restore factory defaults** sets it back to 0 cm by removing that line again
(the rest of `SARndbox.cfg`, such as the water speed and camera settings, is
left alone).

**Edge mask** (phase 4) blacks out everything the projector would draw outside
the box. SARndbox has no setting for this: it draws the whole camera image
through the projector, so the box rim comes out as snow and the floor around the
box as sea level, and tightening the elevation range does not blank anything.
The mask is a small Vrui plugin, `SandboxMask/`, that `run-sandbox.sh` loads into
SARndbox with `-vislet SandboxMask etc/SARndbox-2.8 ;`. It reads `BoxLayout.txt`
and `ProjectorMatrix.dat`, projects the four measured corners onto the screen the
same way SARndbox projects the sand, grows that rectangle by a margin per edge and
paints everything outside it black. Because it starts from the calibration files
at every start, redoing the corners or the projector never breaks it. (Vrui draws
plugins before the application, so the plugin writes its black into the depth
buffer at the near plane; the sand surface drawn afterwards fails the depth test
there and stays black.)

The margins live in `etc/SARndbox-2.8/EdgeMask.cfg`:

```
enabled 1
left 1.5        # cm outside the measured corners: positive shows more,
right 1.5       # negative hides more
bottom 0
top -0.5
highlight left  # optional: outline plus this edge in colour, for 30 s
```

The edges are named as the camera sees the corners; the wizard shows them by
where they land on the screen. In the wizard the phase shrinks the window to a
small always-on-top panel in the middle of the screen, so the sandbox stays
visible around it, and draws the mask as a diagram. The panel takes the focus
back if the sandbox window is clicked, until Done is pressed. Each edge has - and + buttons
(step 0.5, 1 or 5 cm), or use the arrow keys: up/down pick an edge, left/right
move it, Shift for five steps. As with the Color height there is no Save button:
every change is written to `EdgeMask.cfg` after a short pause, the plugin re-reads
the file within half a second, and the running sandbox draws the edge being moved
in yellow with the whole outline in white. Opening the phase for the first time
creates the file with all margins at 0, which is the measured corner rectangle
itself; "Mask on" switches the mask off without losing the margins. The phase
needs the box corners and the projector calibrated first. Restore factory
defaults removes the file, and the installer keeps it across updates like the
other calibration files. Without the plugin (build failed) Vrui prints "Ignoring
vislet of type SandboxMask", the sandbox runs unmasked and the wizard says so on
the overview. The plugin also takes console commands on stdin (`sandboxMaskInfo`,
`sandboxMaskReload`, `sandboxMaskSet <l> <r> <b> <t>`, `sandboxMaskEnable on|off`,
`sandboxMaskHighlight <edge>|off`) for testing by hand.

**The camera depth lens phase** (per-pixel depth correction) is the UC Davis
"Calibrate Depth Lens" step. A Kinect reads a flat surface as slightly
bowl-shaped, and this measures that distortion from several distances and saves
`DepthCorrection-<camera serial>.dat` next to the camera's intrinsic parameters
in `/usr/local/etc/Vrui-8.0/Kinect-3.10`.

**It is hidden for now.** It needs a large flat board held at several distances,
is only worth doing once per camera, and has not been tried on the sandbox PC, so
the wizard shows the three phases above. Start it with:

```
SANDBOX_CALIB_DEPTH=1 ~/src/SARndbox-2.8/bin/CalibrateSandbox.sh
```

It then appears as phase 1 and the others become phases 2 to 5; the numbers on
screen always follow the phases that are visible. It is pre-ticked only while no
correction file exists, its screen has a **Skip this phase** button, and because
it changes every depth reading it marks the other phases for a redo afterwards,
the same way a projector flip does.

For the duration of that phase the wizard starts RawKinectViewer with
`-mergeConfig etc/SARndbox-2.8/DepthLensTools.cfg` (written on the spot), which
unbinds the plane and corner tools and puts "Calibrate Depth Lens" on keys `1`
(capture this distance) and `2` (compute and save). The SandboxHelper plugin
reports each capture and relays the tool's error popup, so the wizard can count
the captures and explain a failure.

The phase 1 screen shows whether the camera already has a correction and has a
**Delete it and start clean** button that throws the `.dat` file out after a
confirmation (a copy goes to `etc/SARndbox-2.8/backups/`). Deleting is not
needed before recalibrating, because RawKinectViewer always computes from raw
depth values and overwrites the file; it is there to put the camera back to
uncorrected readings. Like calibrating, it marks the other phases for a redo,
and a running sandbox keeps the old correction until it is restarted.

The Kinect configuration directory belongs to root after an install, so
RawKinectViewer cannot write the file. The wizard checks this before the phase
and offers a **Fix permissions** button that runs, through `pkexec` (one password
prompt), the equivalent of:

```
sudo chown -R sandbox /usr/local/etc/Vrui-8.0/Kinect-3.10
```

**Flip projector** (button in the top-right corner) turns the projector image
upside down for good, the way Display Settings would, for a sandbox viewed from
the far side. It asks for confirmation, rotates the output with `xrandr`, saves
the choice in `etc/SARndbox-2.8/display-rotation`, and marks every phase that was
calibrated before the flip as needing a redo (calibration depends on the
orientation). `bin/apply-display-rotation.sh` re-applies the saved rotation at
login (`.config/autostart/sandbox-display-rotation.desktop`) and at the start of
`run-sandbox.sh`, so it survives reboots. Press the button again to go back to
normal, which again needs a recalibration.

**Panel size.** The wizard draws inside a centred panel that takes 66% of the
screen, with a dark surround, so the content lands on the sand rather than on
the box edges. Change it with `--scale 0.5` to `--scale 1.0` (full screen) or
`SANDBOX_CALIB_SCALE`.

**SandboxHelper plugin.** (SandboxMask, above, is built and installed the same
way.) RawKinectViewer has no command-line switch for
"Average Frames", and the plane tool needs it. `SandboxHelper/` is a small Vrui
plugin (a "vislet", built by the install script against the installed Vrui and
put into Vrui's `VRVislets` directory) that the wizard loads into the tools with
`-vislet SandboxHelper ;`. It adds four console commands on stdin:
`sandboxAverage on|off` presses the Average Frames menu entry and prints
`SandboxHelper: average frame ready` when the capture dialog has gone,
`sandboxMessage <text>` replaces the open popups with a new one instead of
stacking them, `sandboxCloseMessages`, and `sandboxWatch on|off`, which reports
each average frame capture (`capture started` / `capture done`) and every error
popup the application shows. With the plugin the wizard captures the flat sand
by itself as soon as the camera connects, tells the operator when to start
dragging, offers "Capture the sand again" on the running screen, and in phase 1
counts the depth captures and reads back the "Calibrate Depth Lens" error
message. Without it (build failed, or `SANDBOX_CALIB_VISLET=none`) the wizard
falls back to the manual right-click instructions and stops counting, but every
phase still works. The UC Davis code is not touched.

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
