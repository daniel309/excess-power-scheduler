#!/bin/bash

if [[ $EUID -ne 0 ]]; then
    echo "$0 is not running as root. Try using sudo."
    exit 2
fi

echo "install required packages..."
apt-get update
apt-get -q -y install git python-pip python3-pip
echo "...done"

echo "check for initial installation"
if [ ! -d /opt/excess-power-scheduler ]; then
	cd /opt/
	git clone https://github.com/daniel309/excess-power-scheduler.git --branch master
	echo "... git cloned"
else
	echo "updating software..."
    cd /opt/excess-power-scheduler/
    git pull
    echo "...done"
fi


echo "installing pymodbus"
sudo pip install  -U pymodbus

chmod +x /opt/excess-power-scheduler/main.py

echo "checking cronjob"
if grep -Fxq "/opt/excess-power-scheduler/main.py" /etc/crontab
then
	echo "...ok"
else
    echo "installing cronjob..."
	echo "0 6-22 * * * /opt/excess-power-scheduler/main.py" >> /etc/crontab+
    echo "...done"


