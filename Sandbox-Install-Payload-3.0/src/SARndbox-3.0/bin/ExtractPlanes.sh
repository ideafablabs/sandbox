/home/sandbox/src/Kinect-3.10/bin/RawKinectViewer -compress 0 > /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/planes.txt

if grep -q 'Camera-space plane equation: x *' /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/planes.txt;
then
    grep 'Camera-space plane equation: x *' /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/planes.txt | sed 's/Camera-space plane equation: x \* //' | sed 's/=/,/' > /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.tmp
    tail -n +2 /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.txt >> /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.tmp
    mv /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.tmp /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.txt;
else
    echo "Failed"
    sleep 2;
fi

rm /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/planes.txt

