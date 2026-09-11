#!/bin/bash
# Launches the sandbox calibration wizard (CalibrateSandbox.py), which wraps
# the three UC Davis calibration steps (base plane, box corners, projector)
# into one window. Shows an error dialog if the wizard cannot start.
DIR=/home/sandbox/src/SARndbox-2.8
LOG=$DIR/etc/SARndbox-2.8/calibration.log

# Remember the display orientation: the wizard's "Flip view" rotates the
# projector output, and it must be back to normal when the wizard is gone.
display_state() {
    xrandr --current 2>/dev/null | grep ' connected' | head -1 | \
        sed -nE 's/^([^ ]+) connected( primary)? [0-9]+x[0-9]+\+[0-9]+\+[0-9]+( (normal|left|inverted|right))? \(.*/\1 \4/p'
}
read -r DISPLAY_OUTPUT DISPLAY_ROTATION <<< "$(display_state)"
DISPLAY_ROTATION=${DISPLAY_ROTATION:-normal}

cd "$DIR" || exit 1
python3 "$DIR/bin/CalibrateSandbox.py" "$@" 2>>"$LOG"
rc=$?

if [ -n "$DISPLAY_OUTPUT" ]; then
    read -r _ ROTATION_NOW <<< "$(display_state)"
    if [ "${ROTATION_NOW:-normal}" != "$DISPLAY_ROTATION" ]; then
        xrandr --output "$DISPLAY_OUTPUT" --rotate "$DISPLAY_ROTATION"
    fi
fi

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
