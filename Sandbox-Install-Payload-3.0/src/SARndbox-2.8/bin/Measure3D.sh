#! /bin/bash

/home/sandbox/src/Kinect-3.10/bin/RawKinectViewer -compress 0 > /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/positions.txt

n=$(wc -l < /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/positions.txt)
if [[ $n -eq 5 ]];
then
    head -1 /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.txt > /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.tmp
    tail -n +2 /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/positions.txt >> /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.tmp
    mv /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.tmp /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/BoxLayout.txt;
else
    echo "Failed"
    sleep 2;
fi

rm /home/sandbox/src/SARndbox-2.8/etc/SARndbox-2.8/positions.txt
