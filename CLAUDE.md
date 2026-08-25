# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Azure Container App (`demo.bryansmith.co.za`) fronting many lightweight POC demos behind one Caddy reverse proxy. Each demo either lives in its own repo, pulled in here as a git submodule under `apps/<slug>`, or (for demos with no separate repo) directly under `static/demos/<slug>/`. See `README.md`'s "Adding a demo" section for the exact playbook to add one - don't duplicate it here, but do read it before touching `Caddyfile`, `Dockerfile`, or `demos.json`.

Infra choices here are deliberately POC-scale (one shared Container App, on-demand process spawning instead of one service each) to keep hosting cost near zero - not a reflection of what an enterprise setup would look like. Don't suggest enterprise patterns (per-demo services, managed process orchestration, etc.) as "improvements."

## Architecture

- **`launcher.py`** - a stdlib-only process manager. Caddy calls it via `forward_auth` at `/activate/<slug>` before proxying a request; on first hit it starts that demo's process, polls the port until ready (90s timeout), and later reaps it if idle (`IDLE_TIMEOUT`, default 300s) or CPU-abandoned (`ABANDON_TIMEOUT`, default 3600s). Its reaper decision table is the part most worth testing carefully - getting it wrong is either a visible outage or a slow memory leak - covered by `tests/test_launcher.py`.
- **`demos.json`** - the registry `launcher.py` actually manages: slug, `kind` (`"api"` for a stdlib JSON backend + static frontend), entrypoint path, port. Only demos that need a live backend process are listed here; pure static/JS demos aren't, since Caddy's `file_server` serves them straight off disk with no process involved.
- **`Caddyfile`** - per-demo routing. A demo with a backend process uses `handle` + `forward_auth` + `reverse_proxy`; a pure static demo uses `handle_path` + `file_server` (no process, no `forward_auth`).

## Why `demos.json` says `kind: "api"`, not `"streamlit"`

Despite the README calling out Streamlit as the original choice, the two Python demos here (`momentum-factor`, `factor-regression`) no longer run Streamlit - they were rewritten as stdlib-only JSON APIs (`app/api.py`) with static frontends. The trigger: `st.dataframe`'s pyarrow-based serialization segfaulted the *second* time a Streamlit session ran, under a `pandas`/`pyarrow` version combination that used to require a `pyarrow<19` pin to avoid. That pin is gone now - once the interactive layer moved off Streamlit, the SIGSEGV trigger went with it, and both demos' `pyproject.toml` now allow `pyarrow>=24`. If you see anything (docs, old notes) still referencing Streamlit or a `pyarrow<19` cap for these two demos, it's stale.

## The shared `uv` workspace

`pyproject.toml` at the root declares `[tool.uv.workspace] members = ["apps/momentum-factor", "apps/factor-regression"]` - those two share one resolved venv instead of installing pandas/numpy/matplotlib/pyarrow twice over. `nn-foundations` and `security-anti-patterns` are **not** workspace members (nn-foundations' Python side is a reference implementation that's never deployed; security-anti-patterns has no Python at all) - don't add them to the workspace without a reason, and don't expect `uv sync --all-packages` at the root to touch either.

**Path-baking gotcha**: `uv` venvs bake in their build `WORKDIR` into script shebangs and aren't relocatable. The `Dockerfile`'s `apps-builder` stage's `WORKDIR /app` must match the final stage's `WORKDIR /app` exactly, or the baked venv breaks at runtime. Keep this in mind if you ever restructure the Dockerfile's stages.

## Local dev (`dev.sh`) - what live-refreshes vs. what needs a restart/rebuild

`docker build` + `docker run` bakes everything into the image; any edit needs a rebuild to show up. `./dev.sh` instead bind-mounts source over the built image on **http://localhost:8090** (not 8080 - see the comment in `dev.sh`), but the mounts are asymmetric and it's easy to assume more live-reloads than actually happens:

- **Static/JS demos + landing page** (`static/`, `apps/security-anti-patterns/`): edit and refresh the browser, no restart needed - Caddy's `file_server` reads from disk per request, and the whole `apps/security-anti-patterns` directory is mounted (it has no baked `.venv` to protect).
- **`Caddyfile` / `demos.json` / `launcher.py`**: edits need `docker restart <container>`, not a rebuild.
- **Streamlit-era demo source** (`app/`, `src/`, and for momentum-factor/factor-regression also `assets/`, `config/`, `data/`): only those subpaths are mounted, *deliberately* leaving each demo's baked `.venv` alone - mounting the whole `apps/<slug>` would shadow the image's `.venv` with the bare submodule checkout (no `.venv`) and the demo would fail to start. There's also no file watcher (see `launcher.py`'s idle-reaping design), so these need a restart too, not just a refresh.
- **Dependency changes** (`pyproject.toml`/`uv.lock` in any app): need a full rebuild - venvs are built once at image-build time.

## Submodules

Four, each its own GitHub repo under `apps/<slug>`: `momentum-factor`, `factor-regression`, `nn-foundations`, `security-anti-patterns`. Each has its own `.claude/skills/verify` and `CLAUDE.md` - read those before working inside `apps/<slug>`, they carry demo-specific context this file doesn't repeat.

**`DEMO_SITE_PAT` gotcha**: a submodule repo that should auto-publish on push to its own `main` needs a `.github/workflows/bump-demo-site.yml` (modeled on momentum-factor's) with a `DEMO_SITE_PAT` secret - a fine-grained PAT scoped to just this repo with Contents: Read and write, used to push the submodule-pointer bump commit here. If that PAT is missing or has expired, the bump workflow fails *silently* from demo-site's point of view - the submodule's own repo has moved on, but demo-site's pointer (and therefore the deployed image) is stuck on the old commit with no visible error here. If a demo seems to be running stale code, check the PAT and the submodule repo's Actions tab before anything else.

## Verification

See `.claude/skills/verify` for the full dispatch (root `unittest` suite, per-submodule pytest where one exists, manual smoke pass via `dev.sh`). There's no single command that verifies everything - check the skill for which step applies to what you changed.

For a change scoped to one submodule, run that submodule's own `verify` skill/pytest only, not the root suite plus every other submodule's - reserve the full dispatch for cross-cutting changes (`launcher.py`, `Caddyfile`, `demos.json`) or a final pre-done pass.

## CI

`.github/workflows/deploy.yml` runs on push to `main`: a `test` job (root `unittest` suite + `uv run pytest`) gates `build-and-deploy`, which builds the image, pushes to `ghcr.io`, and updates the Azure Container App.

`uv run pytest` from the root has no `testpaths` restriction, so pytest's plain filesystem discovery picks up every `apps/*/tests/` directory it finds - not just the two `uv` workspace members. In practice: momentum-factor and factor-regression run fully (their deps are in the shared venv); nn-foundations' non-torch tests also run and pass, but `test_torch_parity.py` gets skipped (`torch` isn't installed here, since nn-foundations isn't a workspace member) rather than failing outright - pytest's import-skip handles that gracefully. `security-anti-patterns` has no Python at all, so nothing of its is collected. Don't assume this root job is a substitute for nn-foundations' own CI (which does install `torch` and runs the full suite, including parity) - it's an incidental bonus, not the thing actually gating that repo.
