#!/bin/bash
# use the lava shader file

cp share/SARndbox-2.8/Shaders/SurfaceAddWaterColor-Lava.fs share/SARndbox-2.8/Shaders/SurfaceAddWaterColor.fs
echo "waterAttenuation 0.99" >share/SARndbox-2.8/Control.fifo

