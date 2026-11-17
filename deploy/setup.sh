#!/usr/bin/env bash
set -euo pipefail
cd /data/workspace/darya
umask 077
mkdir -p data
chmod 700 data
if [ ! -d .venv ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -q torch==2.15.0+cpu --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -q -r requirements.lock
if [ ! -f .env ]; then
  : "${PUBLIC_ORIGINS:?Set PUBLIC_ORIGINS to the comma-separated HTTPS origins allowed to access the panel}"
cat > .env <<ENV
HOST=0.0.0.0
PORT=8790
COOKIE_SECURE=true
PUBLIC_ORIGINS=${PUBLIC_ORIGINS}
TELEGRAM_PORT=5222
DATA_DIR=/data/workspace/darya/data
ENV
fi
.venv/bin/python -m unittest discover -s tests -q
sudo install -m 644 deploy/darya.service /etc/systemd/system/darya.service
sudo systemctl daemon-reload
sudo systemctl enable --now darya.service
sudo systemctl restart darya.service
systemctl is-active darya.service
