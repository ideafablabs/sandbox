#!/bin/bash
# use the water shader file
cd ~/src/SARndbox-2.8/
# apply the projector flip chosen in Calibrate Sandbox (no-op when not flipped)
./bin/apply-display-rotation.sh --now
# make sure X does not blank the screen or sleep the projector during a session
./bin/disable-screen-blanking.sh
cp share/SARndbox-2.8/Shaders/SurfaceAddWaterColor-Water.fs share/SARndbox-2.8/Shaders/SurfaceAddWaterColor.fs
# run the sandbox software Look at the SARndbox.cfg file in ~/.config/Vrui-8.0/Applications to see how the
# keys and buttons are mapped.
# -vislet SandboxMask loads the edge mask plugin (SandboxMask/): it blacks out everything the
# projector would draw outside the box, using etc/SARndbox-2.8/EdgeMask.cfg from the wizard's
# Edge mask phase. Vrui strips the vislet arguments before SARndbox sees them; without the
# plugin installed Vrui prints "Ignoring vislet" and the sandbox runs unmasked.
./bin/SARndbox -evr -0.01 -uhm -fpv -cp share/SARndbox-2.8/Control.fifo -vislet SandboxMask etc/SARndbox-2.8 ';'
