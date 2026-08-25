---
name: verify
description: Project-specific verification steps for demo-site — run launcher.py's own tests, dispatch to each apps/<slug> submodule's own suite, and smoke-check the running proxy before calling a change done.
---

# Verify

demo-site itself has no Python dependencies (see `pyproject.toml`) — it's a Caddy reverse proxy plus `launcher.py`, a stdlib-only lazy-start/idle-reap process manager, fronting demos that live under `apps/<slug>` as git submodules. There's no single test command that covers everything; verification is a dispatch across the root and whichever submodule(s) you touched.

## Steps

1. **Root suite** (always run this): `python -m unittest discover tests`. This covers `launcher.py`'s reaper decision table — the logic that decides whether to kill or keep a visitor's demo process. Requires nothing installed (stdlib only).
2. **If you touched a specific `apps/<slug>`**, run *that* submodule's own suite instead of guessing:
   - Has a `pyproject.toml` (`momentum-factor`, `factor-regression`, `nn-foundations`): `cd apps/<slug> && uv sync && uv run pytest`. For momentum-factor/factor-regression specifically, they share demo-site's root `uv` workspace venv, so `uv sync --all-packages && uv run pytest` from the repo root also works and covers both at once.
   - No `pyproject.toml` (`security-anti-patterns`): it's pure static HTML/CSS/JS — there's nothing automated to run. Skip straight to the manual pass below.
   - See that submodule's own `.claude/skills/verify` for its full verification steps (its manual/API-specific checks aren't duplicated here).
3. **Manual smoke pass**: `./dev.sh` (builds once if needed, live-refresh dev server on http://localhost:8090 — not 8080, see `dev.sh`'s comment for why). Open the landing page and each demo's `/demos/<slug>` path, confirm `launcher.py` starts the process on first hit and Caddy proxies it correctly. Watch the container logs for reaper/launch errors.
4. **If you touched `Caddyfile`, `demos.json`, or `launcher.py`** specifically: these need a container restart to pick up in `dev.sh` (no file watcher), and are exactly what step 1's unit tests + step 3's manual pass are meant to catch — don't skip either.

## Scope note

Docs-only changes, or changes touching only `.github/`, `.claude/`, or `README.md`, don't need the manual pass — step 1 (and step 2 if a specific submodule's docs changed) is enough.
