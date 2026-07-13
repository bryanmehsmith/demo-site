# One builder stage per Streamlit demo submodule, each building its own isolated
# .venv from its own pyproject.toml/uv.lock. The venv's shebangs bake in
# WORKDIR, so it must match the final image path exactly (uv venvs aren't relocatable).
# To add another demo:
#   1. git submodule add <repo-url> apps/<slug>
#   2. duplicate the stage below for apps/<slug>, with WORKDIR /app/apps/<slug>
#   3. add `COPY --from=<slug>-builder /app/apps/<slug> /app/apps/<slug>` in the final stage
#   4. add an entry to demos.json and a handle_path block to Caddyfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS momentum-factor-builder
WORKDIR /app/apps/momentum-factor
COPY apps/momentum-factor/pyproject.toml apps/momentum-factor/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY apps/momentum-factor/ .
RUN uv sync --frozen --no-dev

FROM caddy:2 AS caddy

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
COPY --from=momentum-factor-builder /app/apps/momentum-factor /app/apps/momentum-factor
COPY static/ /app/static/
COPY Caddyfile demos.json entrypoint.sh launch_demos.py /app/
RUN chmod +x /app/entrypoint.sh && chown -R appuser:appuser /app
USER appuser
EXPOSE 8080
CMD ["/app/entrypoint.sh"]
