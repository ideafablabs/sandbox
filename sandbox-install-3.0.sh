#!/bin/bash
# Sandbox install / update script, version 3.0
# For SARndbox-2.8, Kinect-3.10 and Vrui-8.0 on Linux Mint (Cinnamon).
#
# Fresh machine:    builds and installs Vrui, Kinect and SARndbox, downloads the camera
#                   calibration, then installs the payload (desktop icons, Vrui configs,
#                   helper scripts, the Calibrate Sandbox wizard) and adjusts desktop settings.
# Existing install: skips whatever is already built, refreshes the payload and KEEPS the
#                   sandbox calibration (BoxLayout.txt and ProjectorMatrix.dat). Every file the
#                   payload replaces is copied to ~/sandbox-backup-<date> first. Safe to re-run.
#
# Usage:
#   wget -O sandbox-install-3.0.sh https://github.com/ideafablabs/sandbox/raw/main/sandbox-install-3.0.sh
#   bash sandbox-install-3.0.sh [options]
#
# Options:
#   --payload-only         only refresh the payload (no builds, no camera calibration)
#   --force-build          rebuild Vrui, Kinect and SARndbox even if they are installed
#   --recalibrate-camera   run "KinectUtil getCalib" again even if calibration data exists
#   --skip-settings        leave the Cinnamon screensaver / power / background settings alone
#   --local-payload FILE   use a local payload tarball instead of downloading it
#   --icon-zoom LEVEL      desktop icon size: standard, large, larger (default) or largest
#   -h, --help             show this text

set -o pipefail

PAYLOAD_URL=https://github.com/ideafablabs/sandbox/raw/main/Sandbox-Install-Payload-3.0.tar.gz
UCD=https://web.cs.ucdavis.edu/~okreylos/ResDev
SANDBOX_DIR=$HOME/src/SARndbox-2.8
ETC_DIR=$SANDBOX_DIR/etc/SARndbox-2.8
FIFO=$SANDBOX_DIR/share/SARndbox-2.8/Control.fifo
KINECT_CALIB_GLOB=/usr/local/etc/Vrui-8.0/Kinect-3.10/IntrinsicParameters-*.dat
STAMP=$(date +%Y%m%d-%H%M%S)

PAYLOAD_ONLY=0; FORCE_BUILD=0; RECALIBRATE=0; SKIP_SETTINGS=0; LOCAL_PAYLOAD=""
DESKTOP_ICON_ZOOM=${DESKTOP_ICON_ZOOM:-larger}   # desktop icon size (see --icon-zoom)
DESKTOP_FONT_SIZE=${DESKTOP_FONT_SIZE:-13}       # point size of the desktop icon labels
while [ $# -gt 0 ]; do
    case "$1" in
        --payload-only)       PAYLOAD_ONLY=1 ;;
        --force-build)        FORCE_BUILD=1 ;;
        --recalibrate-camera) RECALIBRATE=1 ;;
        --skip-settings)      SKIP_SETTINGS=1 ;;
        --local-payload)      shift; LOCAL_PAYLOAD=$(readlink -f "$1") ;;
        --icon-zoom)          shift; DESKTOP_ICON_ZOOM=$1 ;;
        -h|--help)            sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

say()  { echo; echo "==> $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }
SUMMARY=()
note() { SUMMARY+=("$*"); }

cd "$HOME" || die "cannot cd to $HOME"
mkdir -p "$HOME/src"

if [ $PAYLOAD_ONLY -eq 0 ]; then
    # ------------------------------------------------------------------ Vrui
    if [ $FORCE_BUILD -eq 0 ] && [ -x /usr/local/bin/XBackground ]; then
        say "Vrui is already installed, skipping the build"
        note "Vrui:               already installed, skipped"
    else
        say "Building and installing Vrui (this takes a while)"
        wget -O Build-Ubuntu.sh "$UCD/Vrui/Build-Ubuntu.sh" || die "could not download Build-Ubuntu.sh"
        bash Build-Ubuntu.sh || die "the Vrui build failed"
        note "Vrui:               built and installed"
    fi

    # ------------------------------------------------------------------ Kinect
    if [ $FORCE_BUILD -eq 0 ] && [ -x /usr/local/bin/RawKinectViewer ]; then
        say "Kinect-3.10 is already installed, skipping the build"
        note "Kinect-3.10:        already installed, skipped"
    else
        say "Building and installing Kinect-3.10"
        cd "$HOME/src" || die "cannot cd to ~/src"
        wget -O Kinect-3.10.tar.gz "$UCD/Kinect/Kinect-3.10.tar.gz" || die "could not download Kinect-3.10"
        tar xfz Kinect-3.10.tar.gz || die "could not unpack Kinect-3.10"
        cd Kinect-3.10 && make && sudo make install && sudo make installudevrules || die "the Kinect build failed"
        ls /usr/local/bin
        note "Kinect-3.10:        built and installed"
        cd "$HOME"
    fi

    # ------------------------------------------------------------------ SARndbox
    if [ $FORCE_BUILD -eq 0 ] && [ -x "$SANDBOX_DIR/bin/SARndbox" ]; then
        say "SARndbox-2.8 is already built, skipping the build"
        note "SARndbox-2.8:       already built, skipped"
    else
        say "Building SARndbox-2.8"
        cd "$HOME/src" || die "cannot cd to ~/src"
        wget -O SARndbox-2.8.tar.gz "$UCD/SARndbox/SARndbox-2.8.tar.gz" || die "could not download SARndbox-2.8"
        tar xfz SARndbox-2.8.tar.gz || die "could not unpack SARndbox-2.8"
        cd SARndbox-2.8 && make || die "the SARndbox build failed"
        ls ./bin
        note "SARndbox-2.8:       built"
        cd "$HOME"
    fi

    # ------------------------------------------------------------------ camera intrinsic calibration
    if [ $RECALIBRATE -eq 0 ] && ls $KINECT_CALIB_GLOB >/dev/null 2>&1; then
        say "3D camera calibration data is already present, skipping (use --recalibrate-camera to redo)"
        note "Camera calibration: kept"
    else
        say "3D camera calibration"
        read -p "Plug in your 3D camera and press Enter to continue"
        if sudo /usr/local/bin/KinectUtil getCalib 0; then
            note "Camera calibration: downloaded from the camera"
        else
            note "Camera calibration: FAILED - plug in the camera and run: sudo KinectUtil getCalib 0"
        fi
    fi
fi

# ---------------------------------------------------------------------- python3-tk (wizard)
if dpkg -s python3-tk >/dev/null 2>&1; then
    note "python3-tk:         present"
else
    say "Installing python3-tk for the calibration wizard"
    if sudo apt-get install -y python3-tk; then
        note "python3-tk:         installed"
    else
        note "python3-tk:         install FAILED - run: sudo apt-get install python3-tk"
    fi
fi

# ---------------------------------------------------------------------- payload
say "Fetching the payload"
if [ -n "$LOCAL_PAYLOAD" ]; then
    PAYLOAD=$LOCAL_PAYLOAD
    [ -f "$PAYLOAD" ] || die "no such file: $LOCAL_PAYLOAD"
else
    PAYLOAD=$HOME/Sandbox-Install-Payload-3.0.tar.gz
    wget -O "$PAYLOAD" "$PAYLOAD_URL" || die "could not download the payload"
fi
tar tzf "$PAYLOAD" >/dev/null 2>&1 || die "the payload tarball is not readable: $PAYLOAD"

BACKUP_DIR=""
if [ -f "$SANDBOX_DIR/run-sandbox.sh" ] || [ -f "$HOME/Desktop/Sandbox.desktop" ]; then
    BACKUP_DIR=$HOME/sandbox-backup-$STAMP
    say "Existing install found: backing up the files the payload replaces to $BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    tar tzf "$PAYLOAD" | grep -v '/$' | sed 's|^\./||' | while IFS= read -r f; do
        if [ -e "$HOME/$f" ]; then
            mkdir -p "$BACKUP_DIR/$(dirname "$f")"
            cp -a "$HOME/$f" "$BACKUP_DIR/$f"
        fi
    done
fi

say "Installing the payload into $HOME"
tar xzf "$PAYLOAD" -C "$HOME" || die "could not unpack the payload"

if [ -n "$BACKUP_DIR" ]; then
    KEPT=0
    for f in src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.txt src/SARndbox-2.8/etc/SARndbox-2.8/ProjectorMatrix.dat src/SARndbox-2.8/etc/SARndbox-2.8/SARndbox.cfg; do
        if [ -f "$BACKUP_DIR/$f" ]; then
            cp -a "$BACKUP_DIR/$f" "$HOME/$f" && KEPT=$((KEPT + 1))
        fi
    done
    note "Payload:            updated; $KEPT calibration file(s) kept from the existing install"
    note "Backup:             $BACKUP_DIR (icons, configs, scripts and calibration as they were before)"
else
    note "Payload:            installed"
fi

# Desktop icons that older payloads shipped and the wizard replaces
for f in "$HOME/Desktop/ExtractPlanes.desktop" "$HOME/Desktop/Measure3D (copy).desktop" "$HOME/Desktop/CalibrateProjector.desktop"; do
    if [ -e "$f" ]; then
        rm -f "$f" && note "Removed old icon:   $(basename "$f")"
    fi
done

chmod +x "$HOME"/Desktop/*.desktop "$SANDBOX_DIR"/bin/*.sh "$SANDBOX_DIR"/bin/*.py "$SANDBOX_DIR"/*.sh 2>/dev/null

# Control pipe for the colour-map scripts
if [ -p "$FIFO" ]; then
    note "Control.fifo:       present"
else
    rm -f "$FIFO"
    mkdir -p "$(dirname "$FIFO")"
    mkfifo "$FIFO" && note "Control.fifo:       created"
fi

# ---------------------------------------------------------------------- desktop settings
# Background picture, no screensaver / display sleep, sound off, bigger desktop icons.
# DESKTOP_ICON_ZOOM: smallest smaller small standard large larger largest (--icon-zoom)
BG_IMAGE=$HOME/Pictures/ifl-desktop-bg.png

has_schema() { gsettings list-schemas 2>/dev/null | grep -qx "$1"; }
gs() { gsettings set "$@" 2>/dev/null; }

zoom_number() {   # zoom name -> number used in nemo's desktop-metadata file
    case "$1" in
        smallest) echo 0 ;; smaller) echo 1 ;; small) echo 2 ;; standard) echo 3 ;;
        large) echo 4 ;; larger) echo 5 ;; largest) echo 6 ;; *) echo 5 ;;
    esac
}

set_nemo_desktop_zoom() {
    # nemo-desktop keeps the desktop icon size per monitor in this file and only falls
    # back to the gsettings default when the file has no entry, so write it explicitly.
    python3 - "$HOME/.config/nemo/desktop-metadata" "$(zoom_number "$DESKTOP_ICON_ZOOM")" <<'PYEOF2'
import os, re, sys
path, level = sys.argv[1], sys.argv[2]
key = "nemo-icon-view-zoom-level=" + level
try:
    lines = open(path).read().splitlines()
except OSError:
    lines = []
out, state = [], {"in_monitor": False, "done": False, "found": False}

def close_section():
    if state["in_monitor"] and not state["done"]:
        trailing = []
        while out and out[-1].strip() == "":
            trailing.append(out.pop())
        out.append(key)
        out.extend(trailing)

for line in lines:
    if line.startswith("["):
        close_section()
        state["in_monitor"] = re.match(r"\[desktop-monitor-\d+\]", line) is not None
        state["found"] = state["found"] or state["in_monitor"]
        state["done"] = False
        out.append(line)
    elif state["in_monitor"] and line.startswith("nemo-icon-view-zoom-level="):
        out.append(key)
        state["done"] = True
    else:
        out.append(line)
close_section()
if not state["found"]:
    out = ["[desktop-monitor-0]", key, ""] + out
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    f.write("\n".join(out).rstrip("\n") + "\n")
PYEOF2
}

if [ $SKIP_SETTINGS -eq 0 ] && command -v gsettings >/dev/null 2>&1; then
    say "Applying desktop settings"
    # Reach the desktop session's settings even when this script runs over SSH
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$(id -u)/bus" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    fi
    [ -z "${DISPLAY:-}" ] && export DISPLAY=:0
    APPLIED=""

    if has_schema org.cinnamon.desktop.background; then                     # Cinnamon
        gs org.cinnamon.desktop.screensaver idle-activation-enabled false
        gs org.cinnamon.desktop.screensaver lock-enabled false
        gs org.cinnamon.settings-daemon.plugins.power sleep-display-ac 0
        gs org.cinnamon.settings-daemon.plugins.power sleep-display-battery 0
        gs org.cinnamon.settings-daemon.plugins.power button-power shutdown
        gs org.cinnamon.desktop.session idle-delay 0
        gs org.cinnamon.desktop.sound volume-sound-enabled false
        gs org.cinnamon.desktop.background picture-options zoom
        gs org.cinnamon.desktop.background picture-uri "file://$BG_IMAGE"
        APPLIED="Cinnamon"
    fi
    if has_schema org.mate.background; then                                  # MATE
        gs org.mate.screensaver idle-activation-enabled false
        gs org.mate.screensaver lock-enabled false
        gs org.mate.power-manager sleep-display-ac 0
        gs org.mate.session idle-delay 0
        gs org.mate.background picture-options zoom
        gs org.mate.background picture-filename "$BG_IMAGE"
        APPLIED="${APPLIED:+$APPLIED, }MATE"
    fi
    if has_schema org.gnome.desktop.background; then                         # GNOME (also present on Cinnamon)
        gs org.gnome.desktop.background picture-options zoom
        gs org.gnome.desktop.background picture-uri "file://$BG_IMAGE"
        [ -z "$APPLIED" ] && APPLIED="GNOME"
    fi

    # Bigger desktop icons and labels
    if has_schema org.nemo.icon-view; then                                   # Cinnamon's nemo-desktop
        gs org.nemo.icon-view default-zoom-level "$DESKTOP_ICON_ZOOM"
        FONT=$(gsettings get org.nemo.desktop font 2>/dev/null | sed -E "s/ [0-9]+'\$/ $DESKTOP_FONT_SIZE'/")
        [ -n "$FONT" ] && gs org.nemo.desktop font "$FONT"
        set_nemo_desktop_zoom
        if pgrep -x nemo-desktop >/dev/null 2>&1; then
            pkill -x nemo-desktop
            sleep 1
            (setsid nemo-desktop >/dev/null 2>&1 &)
        fi
        note "Desktop icons:      $DESKTOP_ICON_ZOOM, label font size $DESKTOP_FONT_SIZE"
    elif has_schema org.mate.caja.icon-view; then                            # MATE's caja
        gs org.mate.caja.icon-view default-zoom-level "$DESKTOP_ICON_ZOOM"
        FONT=$(gsettings get org.mate.caja.desktop font 2>/dev/null | sed -E "s/ [0-9]+'\$/ $DESKTOP_FONT_SIZE'/")
        [ -n "$FONT" ] && gs org.mate.caja.desktop font "$FONT"
        note "Desktop icons:      $DESKTOP_ICON_ZOOM, label font size $DESKTOP_FONT_SIZE (log out and in to apply)"
    fi

    # Check that the background really took
    BG_NOW=""
    if has_schema org.cinnamon.desktop.background; then
        BG_NOW=$(gsettings get org.cinnamon.desktop.background picture-uri 2>/dev/null)
    elif has_schema org.mate.background; then
        BG_NOW=$(gsettings get org.mate.background picture-filename 2>/dev/null)
    elif has_schema org.gnome.desktop.background; then
        BG_NOW=$(gsettings get org.gnome.desktop.background picture-uri 2>/dev/null)
    fi
    if [ -z "$APPLIED" ]; then
        note "Desktop settings:   no Cinnamon, MATE or GNOME settings found; nothing applied"
    elif [ ! -f "$BG_IMAGE" ]; then
        note "Desktop settings:   applied ($APPLIED) but $BG_IMAGE is missing"
    elif [[ "$BG_NOW" == *ifl-desktop-bg.png* ]]; then
        note "Desktop settings:   applied ($APPLIED); background is $BG_IMAGE"
    else
        note "Desktop settings:   applied ($APPLIED) but the background did not take (reads ${BG_NOW:-nothing}); run this script from a terminal inside the desktop session"
    fi
else
    note "Desktop settings:   skipped"
fi

# ---------------------------------------------------------------------- summary
say "Checking the calibration wizard's paths"
python3 "$SANDBOX_DIR/bin/CalibrateSandbox.py" --check 2>/dev/null || echo "(some paths are missing; see above)"

echo
echo "================ Summary ================"
for line in "${SUMMARY[@]}"; do echo "  $line"; done
echo "========================================="
if [ -n "$BACKUP_DIR" ]; then
    echo "Update finished. If the sandbox is running, close it (Esc) and start it again from the Sandbox icon."
    echo "Your calibration was kept; use the 'Calibrate Sandbox' icon only if you want to re-calibrate."
else
    echo "Install finished. Use the 'Calibrate Sandbox' icon to calibrate, then the 'Sandbox' icon to run."
fi
