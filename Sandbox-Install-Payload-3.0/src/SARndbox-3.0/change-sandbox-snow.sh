#!/bin/bash
# use the snow shader file

cp share/SARndbox-2.8/Shaders/SurfaceAddWaterColor-Snow.fs share/SARndbox-2.8/Shaders/SurfaceAddWaterColor.fs
echo "waterAttenuation 0.00078125" >share/SARndbox-2.8/Control.fifo



