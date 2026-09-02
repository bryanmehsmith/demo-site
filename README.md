# demo-site

Hosts `demo.bryansmith.co.za`, a single Azure Container App that fronts many
lightweight POC demos behind one Caddy reverse proxy. Each demo lives in its
own repo, pulled in here as a git submodule under `apps/<slug>`. All current
demos use static JavaScript frontends served by Caddy's `file_server`.
Momentum Factor and Factor Regression also have small, optional JSON APIs for
fresh market-data requests; their analysis still runs in the browser. A demo
can skip the submodule and live directly under `static/demos/<slug>/` if it
doesn't need its own repo.

Static frontends, on-demand companion APIs, and one shared container keep
infrastructure and hosting cost low. These are POC-scale choices, not a
reflection of what a company or enterprise setup would look like; a company
environment would justify a different strategy.

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
   branch, add a `.github/workflows/bump-demo-site.yml` to *that* repo, modeled
   on the one in the momentum-factor repo.

**Static/JS, no separate repo:** add `static/demos/<slug>/`, link it from
`static/index.html`.

**Static/JS with an optional Python API:**
1. Add the submodule under `apps/<slug>`, add a static frontend
   `handle_path` + `file_server` route, link it from `static/index.html`, and
   mount its frontend in `dev.sh`.
2. Add the submodule to the root `pyproject.toml` uv workspace, copy its
   `pyproject.toml` and source in the Dockerfile's `apps-builder` stage, run
   `uv lock` at the root, and copy the built app into the final image from the
   builder. Use this builder-origin copy instead of the plain static-submodule
   copy described above. The shared workspace venv must remain at
   `/app/.venv` because its shebangs are not relocatable.
3. Add a `kind: "api"` entry to `demos.json` with its entrypoint and port.
4. Before the frontend's catch-all route, add a more specific
   `/demos/<slug>/api/*` block with `forward_auth` and `reverse_proxy`. Use the
   momentum-factor block as the working pattern.
5. Mount the frontend and editable backend source paths in `dev.sh`. These app
   mounts do not affect the shared environment at `/app/.venv`.

**Process-backed Streamlit (supported, but not used by a current demo):**
1. `git submodule add <repo-url> apps/<slug>`. The target repo needs its own
   `pyproject.toml`/`uv.lock` and a Streamlit entrypoint at a known relative path.
2. Add it to the shared uv workspace and Dockerfile using the same build pattern
   as an API-backed demo, then run `uv lock` at the root.
3. Add a `kind: "streamlit"` entry to `demos.json` with its entrypoint and port.
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
- **Static/JS frontends and the landing page** (`static/` and each demo's
  frontend directory):
  edits show up on the next browser refresh, no restart needed, since
  Caddy's `file_server` reads from disk per request. Fully static demos can be
  mounted as a whole because they have no baked `.venv` to protect.
- **`Caddyfile` / `demos.json` / `launcher.py`**: edits need a container
  restart (`docker restart <container>`), not a rebuild.
- **Optional API and Python reference source** (each hybrid demo's `app/`,
  `src/`, `assets/`, `config/`, and `data/`): only
  those source paths are mounted for targeted live editing. The environment is
  shared at `/app/.venv`, outside every app directory, so app mounts do not
  shadow it. Backend source edits need a container restart because the launcher
  does not run a source watcher.
- Changing a demo's dependencies (`pyproject.toml`/`uv.lock`) still needs a
  full rebuild, since venvs are built once at image-build time.
