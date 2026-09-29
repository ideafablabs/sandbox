echo Restoring Defaults ...
cp -v /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.txt.orig /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.txt
cp -v /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/ProjectorMatrix.dat.orig /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/ProjectorMatrix.dat
# the edge mask (wizard phase 4) is not a factory file: set it aside so the sandbox runs unmasked
[ -f /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/EdgeMask.cfg ] && mv -v /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/EdgeMask.cfg /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/EdgeMask.cfg.bak
echo Finished ...
sleep 3
