# One shared uv workspace venv for all three Python demo submodules, instead of
# an isolated venv per app. Streamlit, pandas, numpy, pyarrow and matplotlib were
# previously installed three times over; the workspace lets uv resolve and
# install them once. The venv's shebangs bake in WORKDIR, so it must match the
# final image path exactly (uv venvs aren't relocatable).
# To add another Streamlit demo:
#   1. git submodule add <repo-url> apps/<slug>
#   2. add apps/<slug> to [tool.uv.workspace] members in the root pyproject.toml,
#      run `uv lock` at the root, and commit the updated uv.lock
#   3. add COPY lines for apps/<slug>/pyproject.toml and apps/<slug>/ in the
#      apps-builder stage below, and for /app/apps/<slug> in the final stage
#   4. add an entry to demos.json, and a Caddyfile `handle` block containing both
#      the forward_auth (which starts the demo on demand, and must pass
#      X-Original-Uri) and the reverse_proxy to its port. Copy an existing block;
#      the pieces are load-bearing and explained there.
# A static/JS demo submodule (no Python, no process) skips all of the above:
# no builder involvement, just a plain `COPY apps/<slug>/ /app/apps/<slug>/` in
# the final stage, and a Caddyfile `handle_path` + `file_server` block.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS apps-builder

WORKDIR /app

COPY pyproject.toml uv.lock ./

COPY apps/momentum-factor/pyproject.toml ./apps/momentum-factor/pyproject.toml
COPY apps/factor-regression/pyproject.toml ./apps/factor-regression/pyproject.toml

RUN uv sync \
    --frozen \
    --no-dev \
    --all-packages \
    --no-install-workspace

COPY apps/momentum-factor/ ./apps/momentum-factor/
COPY apps/factor-regression/ ./apps/factor-regression/

RUN uv sync \
    --frozen \
    --no-dev \
    --all-packages

FROM caddy:2 AS caddy

FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}"

COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy

COPY --from=apps-builder --chown=1000:1000 \
    /app/.venv /app/.venv

COPY --from=apps-builder --chown=1000:1000 \
    /app/apps/momentum-factor /app/apps/momentum-factor

COPY --from=apps-builder --chown=1000:1000 \
    /app/apps/factor-regression /app/apps/factor-regression

COPY --chown=1000:1000 \
    apps/security-anti-patterns/ /app/apps/security-anti-patterns/

# nn-foundations is a fully static demo (see Caddyfile): only its built frontend
# ships, never its Python reference implementation (src/, tests/, pyproject.toml),
# which stays in the repo purely for local fixture generation and parity testing.
COPY --chown=1000:1000 \
    apps/nn-foundations/frontend/ /app/apps/nn-foundations/frontend/

COPY --chown=1000:1000 static/ /app/static/

COPY --chown=1000:1000 \
    Caddyfile demos.json launcher.py /app/

COPY --chown=1000:1000 --chmod=755 \
    entrypoint.sh /app/entrypoint.sh

USER appuser

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
