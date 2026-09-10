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
while [ $# -gt 0 ]; do
    case "$1" in
        --payload-only)       PAYLOAD_ONLY=1 ;;
        --force-build)        FORCE_BUILD=1 ;;
        --recalibrate-camera) RECALIBRATE=1 ;;
        --skip-settings)      SKIP_SETTINGS=1 ;;
        --local-payload)      shift; LOCAL_PAYLOAD=$(readlink -f "$1") ;;
        -h|--help)            sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
    for f in src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.txt src/SARndbox-2.8/etc/SARndbox-2.8/ProjectorMatrix.dat; do
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
if [ $SKIP_SETTINGS -eq 0 ] && command -v gsettings >/dev/null 2>&1; then
    say "Applying desktop settings (screensaver off, no display sleep, sound off, background)"
    gsettings set org.cinnamon.desktop.screensaver idle-activation-enabled false 2>/dev/null
    gsettings set org.cinnamon.desktop.screensaver lock-enabled false 2>/dev/null
    gsettings set org.cinnamon.settings-daemon.plugins.power sleep-display-ac 0 2>/dev/null
    gsettings set org.cinnamon.settings-daemon.plugins.power sleep-display-battery 0 2>/dev/null
    gsettings set org.cinnamon.settings-daemon.plugins.power button-power shutdown 2>/dev/null
    gsettings set org.cinnamon.desktop.session idle-delay 0 2>/dev/null
    gsettings set org.cinnamon.desktop.sound volume-sound-enabled false 2>/dev/null
    gsettings set org.cinnamon.desktop.background picture-uri "file://$HOME/Pictures/ifl-desktop-bg.png" 2>/dev/null
    note "Desktop settings:   applied"
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
