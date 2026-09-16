#!/bin/bash
# Turns off the X server's own screen blanking and DPMS power saving for this
# login session, so the projector never goes dark while the sandbox is running.
#
# Cinnamon's screensaver, screen lock, display sleep and suspend are switched off
# separately by sandbox-install-3.0.sh through gsettings. This covers the layer
# below that, which those settings do not reach and which comes back whenever the
# X server is restarted.
#
# Run from the login autostart entry (.config/autostart/sandbox-no-screen-blanking.desktop)
# and again at the start of run-sandbox.sh. Safe to run any number of times.
[ -n "$DISPLAY" ] || export DISPLAY=:0
command -v xset >/dev/null 2>&1 || exit 0

xset s off        # no screen saver timeout
xset s noblank    # and do not blank the screen if one is triggered anyway
xset -dpms        # no monitor standby, suspend or off
exit 0
