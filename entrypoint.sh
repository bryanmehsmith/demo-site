#!/bin/sh
set -e

# The launcher starts demos on demand, so Caddy must not take traffic until the
# launcher is answering: Caddy asks it (via forward_auth) about every demo
# request, and would return 502 for all of them if it were not up yet.
# Uses the shared workspace venv's Python explicitly, though PATH would resolve
# to it anyway since it's prepended in the Dockerfile.
/app/.venv/bin/python /app/launcher.py &

i=0
while [ "$i" -lt 100 ]; do
	if /app/.venv/bin/python -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('127.0.0.1', 9000)))" 2>/dev/null; then
		echo "launcher is ready"
		break
	fi
	i=$((i + 1))
	sleep 0.1
done

exec caddy run --config /app/Caddyfile --adapter caddyfile
