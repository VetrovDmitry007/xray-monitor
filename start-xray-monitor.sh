#!/bin/bash
set -e

systemctl stop xray-monitor
cd /root/Project/xray-monitor
git pull
systemctl start xray-monitor