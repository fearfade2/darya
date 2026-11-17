#!/bin/bash
set -eu
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq ffmpeg
fi
