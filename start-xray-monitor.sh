#!/bin/bash
set -e

APP_DIR="/root/Project/xray-monitor"
REPO_URL="https://github.com/VetrovDmitry007/xray-monitor.git"
BRANCH="dev"
IMAGE_NAME="image-xray-monitor"

mkdir -p /root/Project

if [ ! -d "$APP_DIR/.git" ]; then
    git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
else
    cd "$APP_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull origin "$BRANCH"
fi

cd "$APP_DIR"

docker compose down
docker build -t "$IMAGE_NAME" .
docker compose up -d