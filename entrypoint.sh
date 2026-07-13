#!/bin/sh
set -e
python3 /app/launch_demos.py &
exec caddy run --config /app/Caddyfile --adapter caddyfile
