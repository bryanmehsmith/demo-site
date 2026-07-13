# demo-site

Hosts `demo.bryansmith.co.za` — a single Azure Container App that fronts many
lightweight POC demos behind one Caddy reverse proxy. Static/JS demos are served
directly from `static/demos/<slug>/`; Streamlit demos each live in their own repo,
pulled in here as a git submodule under `apps/<slug>` and run as an internal
process that Caddy proxies to at `/demos/<slug>`.

## Adding a demo

**Static/JS:** add `static/demos/<slug>/`, link it from `static/index.html`.

**Streamlit:**
1. `git submodule add <repo-url> apps/<slug>` — the target repo needs its own
   `pyproject.toml`/`uv.lock` and a Streamlit entrypoint at a known relative path.
2. Add an entry to `demos.json` (slug, entrypoint path, port).
3. Duplicate a builder stage in the `Dockerfile` for the new submodule, and add
   its `COPY --from=<slug>-builder` line in the final stage.
4. Add a `handle_path /demos/<slug>*` block to `Caddyfile`.
5. Link it from `static/index.html`.

Push to `main` — GitHub Actions builds the image, pushes to `ghcr.io`, and
updates the Azure Container App to the new revision.

## Local dev

```
git submodule update --init --recursive
docker build -t demo-site .
docker run -p 8080:8080 demo-site
```

Then open http://localhost:8080.
