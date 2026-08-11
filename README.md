# demo-site

Hosts `demo.bryansmith.co.za`, a single Azure Container App that fronts many
lightweight POC demos behind one Caddy reverse proxy. Each demo lives in its
own repo, pulled in here as a git submodule under `apps/<slug>`. Streamlit
demos run as an internal process that Caddy proxies to at `/demos/<slug>`;
static/JS demos have no process at all, and are served straight off disk by
Caddy's `file_server`. A demo can also skip the submodule and live directly
under `static/demos/<slug>/` if it doesn't need its own repo.

Streamlit was chosen deliberately here to keep infrastructure minimal and
hosting cost low while still getting the analysis across. More broadly, the
coding, architecture, and infrastructure decisions in this repo are POC-scale
choices, not a reflection of what a company/enterprise setup would look like;
a company environment would justify a different, more robust strategy.

## Adding a demo

**Static/JS, own repo (submodule):**
1. `git submodule add <repo-url> apps/<slug>`.
2. Add a `handle_path /demos/<slug>*` block to `Caddyfile` using `root * /app/apps/<slug>`
   and `file_server` (no `forward_auth`, no process to start).
3. Add a plain `COPY apps/<slug>/ /app/apps/<slug>/` line to the `Dockerfile`'s
   final stage (no builder stage needed).
4. Add the mount to `dev.sh` for live-refresh in local dev.
5. Link it from `static/index.html`.
6. If the demo's own repo should auto-publish on every push to its `main`
   branch, add a `.github/workflows/bump-demo-site.yml` to *that* repo (see
   step 6 under Streamlit below).

**Static/JS, no separate repo:** add `static/demos/<slug>/`, link it from
`static/index.html`.

**Streamlit:**
1. `git submodule add <repo-url> apps/<slug>`. The target repo needs its own
   `pyproject.toml`/`uv.lock` and a Streamlit entrypoint at a known relative path.
2. Add an entry to `demos.json` (slug, entrypoint path, port).
3. Duplicate a builder stage in the `Dockerfile` for the new submodule (with a
   matching `WORKDIR /app/apps/<slug>` (uv venvs bake in their build path, so
   it must match the final location), and add its `COPY --from=<slug>-builder`
   line in the final stage.
4. Add a `handle /demos/<slug>*` block to `Caddyfile` (not `handle_path`;
   Streamlit's `--server.baseUrlPath` needs the full prefix kept on the request).
5. Link it from `static/index.html`.
6. If the demo's own repo should auto-publish on every push to its `main`
   branch (like momentum-factor does), add a `.github/workflows/bump-demo-site.yml`
   to *that* repo, modeled on the one in the momentum-factor repo. It needs a
   `DEMO_SITE_PAT` secret (fine-grained PAT scoped to just this repo, Contents:
   Read and write) to push the submodule bump here.

Push to `main` (here, or via an upstream demo repo's auto-bump workflow).
GitHub Actions builds the image, pushes to `ghcr.io`, and updates the Azure
Container App to the new revision.

## Local dev

```
git submodule update --init --recursive
docker build -t demo-site .
docker run -p 8080:8080 demo-site
```

Then open http://localhost:8080.

### Live-refresh while editing

The command above copies everything into the image at build time, so any
edit needs a rebuild to show up. To iterate without rebuilding, run
`./dev.sh` instead: it builds the image once if missing, then runs it with
the source bind-mounted over the image's copies, on **http://localhost:8090**
(not 8080; see the comment in `dev.sh` for why; override with
`DEV_PORT=<port> ./dev.sh` if 8090 is also taken).

What that gets you:
- **Static/JS demos and the landing page** (`static/`, `apps/security-anti-patterns/`):
  edits show up on the next browser refresh, no restart needed, since
  Caddy's `file_server` reads from disk per request. The whole
  `apps/security-anti-patterns` directory is mounted (there's no baked
  `.venv` to protect, unlike the Streamlit demos below).
- **`Caddyfile` / `demos.json` / `launcher.py`**: edits need a container
  restart (`docker restart <container>`), not a rebuild.
- **Streamlit demo source** (each demo's `app/`, `src/`, and, for
  momentum-factor/factor-regression, `assets/`, `config/`, `data/`): only
  those subpaths are mounted, deliberately leaving each demo's baked `.venv`
  from the image alone; mounting the whole `apps/<slug>` would shadow it with
  the bare submodule checkout (no `.venv`) and the demo would fail to start.
  Streamlit's file watcher is disabled in this repo (see `launcher.py`'s
  idle-reaping logic), so source edits also need a container restart, not
  just a refresh.
- Changing a demo's dependencies (`pyproject.toml`/`uv.lock`) still needs a
  full rebuild, since venvs are built once at image-build time.
