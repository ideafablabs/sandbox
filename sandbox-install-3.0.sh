#!/bin/bash
# 5/8/2024 ver 3.0.0 support for SARndbox-2.8, Kinect-3.10, Vrui-8.0
# includes udev usb encoder button support & weather script support
# wget ---
# bash ARSarnd-3.0.sh

cd ~
wget http://web.cs.ucdavis.edu/~okreylos/ResDev/Vrui/Build-Ubuntu.sh
bash Build-Ubuntu.sh

cd ~/src
wget http://web.cs.ucdavis.edu/~okreylos/ResDev/Kinect/Kinect-3.10.tar.gz
tar xfz Kinect-3.10.tar.gz
cd Kinect-3.10
make
sudo make install
sudo make installudevrules
ls /usr/local/bin

cd ~/src
wget http://web.cs.ucdavis.edu/~okreylos/ResDev/SARndbox/SARndbox-2.8.tar.gz
tar xfz SARndbox-2.8.tar.gz
cd SARndbox-2.8
make
ls ./bin

#Kinect Calibration
read -p "Plug in your 3D Camera and Press enter to continue"
sudo /usr/local/bin/KinectUtil getCalib 0

#
cd ~
wget https://github.com/ideafablabs/sandbox/raw/master/Sandbox-Install-Payload-3.0.tar.gz
tar xfz Sandbox-Install-Payload-3.0.tar.gz

mkfifo ~/src/SARndbox-2.8/share/SARndbox-2.8/Control.fifo

#Settings adjustments

#To disable the screensaver:
gsettings set org.cinnamon.desktop.screensaver idle-activation-enabled false

#To disable the power management settings:
gsettings set org.cinnamon.settings-daemon.plugins.power sleep-display-ac 0
gsettings set org.cinnamon.settings-daemon.plugins.power sleep-display-battery 0

#To disable the lock screen:
gsettings set org.cinnamon.desktop.session idle-delay 0

#To set the volume to 0%:
gsettings set org.cinnamon.desktop.sound volume-sound-enabled false

#set the desktop background to the logo
gsettings set org.cinnamon.desktop.background picture-uri file:///usr/sandbox/Picture/ifl-desktop-bg.png

