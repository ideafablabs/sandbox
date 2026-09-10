#!/bin/bash
# Launches the sandbox calibration wizard (CalibrateSandbox.py), which wraps
# the three UC Davis calibration steps (base plane, box corners, projector)
# into one window. Shows an error dialog if the wizard cannot start.
DIR=/home/sandbox/src/SARndbox-2.8
LOG=$DIR/etc/SARndbox-2.8/calibration.log

cd "$DIR" || exit 1
python3 "$DIR/bin/CalibrateSandbox.py" "$@" 2>>"$LOG"
rc=$?
if [ $rc -ne 0 ]; then
    msg="The calibration wizard stopped with error code $rc.

Last lines of $LOG:

$(tail -n 12 "$LOG" 2>/dev/null)"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --width=700 --title="Calibrate Sandbox" --text="$msg"
    else
        echo "$msg" >&2
    fi
fi
exit $rc
