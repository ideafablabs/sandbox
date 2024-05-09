#!/bin/bash
# use the water shader file

cp share/SARndbox-2.8/Shaders/SurfaceAddWaterColor-Water.fs share/SARndbox-2.8/Shaders/SurfaceAddWaterColor.fs
echo "waterAttenuation 0.0078125" >share/SARndbox-2.8/Control.fifo


