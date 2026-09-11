#!/bin/bash
# Re-applies the projector rotation chosen with the calibration wizard's "Flip projector"
# button. The wizard saves it in etc/SARndbox-2.8/display-rotation as "<output> <rotation>"
# (for example "HDMI-1 inverted"). This script runs at login from
# ~/.config/autostart/sandbox-display-rotation.desktop and from run-sandbox.sh.
#
#   apply-display-rotation.sh        wait a few seconds first (login: let the desktop settle)
#   apply-display-rotation.sh --now  apply immediately
DIR=$(cd "$(dirname "$0")/.." && pwd)
FILE=$DIR/etc/SARndbox-2.8/display-rotation

[ -f "$FILE" ] || exit 0
read -r OUTPUT ROTATION < "$FILE"
ROTATION=${ROTATION:-normal}
[ -n "$OUTPUT" ] || exit 0
[ "$1" = "--now" ] || sleep 8

current_rotation() {
    # Prints the rotation word of the given connected output (normal when none is shown).
    xrandr --current 2>/dev/null | awk -v o="$1" '
        $1 == o && $2 == "connected" {
            r = "normal"
            for (i = 3; i <= NF; i++) {
                if ($i ~ /^\(/) break
                if ($i == "normal" || $i == "left" || $i == "inverted" || $i == "right") r = $i
            }
            print r; exit
        }'
}

if ! xrandr --current 2>/dev/null | awk -v o="$OUTPUT" '$1 == o && $2 == "connected" {f = 1} END {exit !f}'; then
    # The saved output is not connected any more (cable moved?): use the first connected one.
    OUTPUT=$(xrandr --current 2>/dev/null | awk '$2 == "connected" {print $1; exit}')
    [ -n "$OUTPUT" ] || exit 0
fi

current=$(current_rotation "$OUTPUT")
[ "${current:-normal}" = "$ROTATION" ] && exit 0
exec xrandr --output "$OUTPUT" --rotate "$ROTATION"
