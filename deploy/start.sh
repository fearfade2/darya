#!/bin/bash
set -eu

# Instabox delivers the managed Secrets file after the guest begins booting.
while [ ! -r /run/instabox/secrets/env ]; do
    sleep 1
done
. /run/instabox/secrets/env
exec /data/workspace/darya/.venv/bin/python /data/workspace/darya/run.py
