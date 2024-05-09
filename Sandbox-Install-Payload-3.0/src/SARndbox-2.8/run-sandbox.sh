#!/bin/bash
# use the water shader file
cd ~/src/SARndbox-2.8/
cp share/SARndbox-2.8/Shaders/SurfaceAddWaterColor-Water.fs share/SARndbox-2.8/Shaders/SurfaceAddWaterColor.fs
# run the sandbox software Look at the SARndbox.cfg file in ~/.config/Vrui-8.0/Applications to see how the
# keys and buttons are mapped
./bin/SARndbox -evr -0.01 -uhm -fpv -cp share/SARndbox-2.8/Control.fifo
