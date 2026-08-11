#!/usr/bin/env bash
# Runs demo-site locally with source bind-mounted over the image's copies, so
# edits show up without a full `docker build`. See README.md "Local dev" for
# what does and doesn't need a container restart under this setup.
set -euo pipefail
cd "$(dirname "$0")"

if ! docker image inspect demo-site >/dev/null 2>&1; then
  echo "No demo-site image found; building once (this is the only build this script needs)..."
  docker build -t demo-site .
fi

# Host port deliberately isn't 8080: on a dev machine running other stacks
# (Postgres, RabbitMQ, etc. under WSL2), something else can already have 8080
# forwarded, and requests then silently hit that instead of this container.
# Override with DEV_PORT=<port> ./dev.sh if 8090 collides too.
docker run --rm -p "${DEV_PORT:-8090}:8080" \
  -v "$(pwd)/static:/app/static" \
  -v "$(pwd)/Caddyfile:/app/Caddyfile" \
  -v "$(pwd)/demos.json:/app/demos.json" \
  -v "$(pwd)/launcher.py:/app/launcher.py" \
  -v "$(pwd)/apps/momentum-factor/app:/app/apps/momentum-factor/app" \
  -v "$(pwd)/apps/momentum-factor/src:/app/apps/momentum-factor/src" \
  -v "$(pwd)/apps/momentum-factor/assets:/app/apps/momentum-factor/assets" \
  -v "$(pwd)/apps/momentum-factor/config:/app/apps/momentum-factor/config" \
  -v "$(pwd)/apps/momentum-factor/data:/app/apps/momentum-factor/data" \
  -v "$(pwd)/apps/factor-regression/app:/app/apps/factor-regression/app" \
  -v "$(pwd)/apps/factor-regression/src:/app/apps/factor-regression/src" \
  -v "$(pwd)/apps/factor-regression/assets:/app/apps/factor-regression/assets" \
  -v "$(pwd)/apps/factor-regression/config:/app/apps/factor-regression/config" \
  -v "$(pwd)/apps/factor-regression/data:/app/apps/factor-regression/data" \
  -v "$(pwd)/apps/nn-foundations/app:/app/apps/nn-foundations/app" \
  -v "$(pwd)/apps/nn-foundations/src:/app/apps/nn-foundations/src" \
  -v "$(pwd)/apps/security-anti-patterns:/app/apps/security-anti-patterns" \
  demo-site
