# Agent Runbook

This file is a quick execution guide for agents working in this repository.
Use it as an operational checklist, not as product documentation.

## Canonical References

- `/Users/talor/github/projects/compressatorium/README.md`
- `/Users/talor/github/projects/compressatorium/RELEASE_NOTES.md`
- `/Users/talor/github/projects/compressatorium/DOCKER-COMPOSE.md`
- `/Users/talor/github/projects/compressatorium/DEPLOYMENT.md`
- `/Users/talor/github/projects/compressatorium/.github/workflows/docker-image.yml`

## Current Workflows

### 1. Local Dev (Web UI)

The frontend is a Svelte 5 + Vite single-page application (SPA) under `src/` (see README §"Frontend Development"). The backend is FastAPI under `app/`. Build output lands in `static/` and is served by FastAPI via the existing `/static` mount.

- Production-style run (FastAPI serves the prebuilt SPA):

```bash
cd /Users/talor/github/projects/compressatorium
npm install && npm run build      # one-time / when src/ changes
./run_dev.sh                       # loads .env.local, bootstraps .venv, starts uvicorn
```

- Hot-reload dev loop (two terminals):

```bash
# Terminal 1 — backend
./run_dev.sh                       # uvicorn on :8080

# Terminal 2 — Vite dev server with HMR (hot module replacement)
npm run dev                        # http://localhost:5173 (proxies /api and /health to :8080)
```

- App URL: `http://localhost:8080` (prod-style) or `http://localhost:5173` (Vite HMR).
- Important behavior: if `COMPRESSATORIUM_VOLUMES` is unset, volumes are auto-discovered under `COMPRESSATORIUM_MOUNT_ROOT/*`.
- The legacy `static/js/app.js` + `static/vendor/standalone.mjs` (Preact + htm) frontend is being decommissioned; do not extend it. All new UI work goes in `src/`.

### 2. Test Workflow

- Run full tests:

```bash
cd /Users/talor/github/projects/compressatorium
pytest -q tests
```

- Run targeted regression suites:

```bash
cd /Users/talor/github/projects/compressatorium
pytest -q tests/test_mode_parity_fixes.py
pytest -q tests/test_volume_discovery.py
```

### 3. Docker Runtime Workflows

- Single-volume Web UI:

```bash
cd /Users/talor/github/projects/compressatorium
docker-compose up -d
```

- Multi-volume Web UI:

```bash
cd /Users/talor/github/projects/compressatorium
docker-compose -f docker-compose.multi-volume.yml up -d
```

- CLI batch mode:

```bash
cd /Users/talor/github/projects/compressatorium
docker-compose -f docker-compose.cli.yml up
```

- Common operations:

```bash
cd /Users/talor/github/projects/compressatorium
docker-compose ps
docker-compose logs -f
docker-compose restart
docker-compose down
```

### 4. Queue/Admin API Workflow

- Health + version checks:

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/api/version
```

- Cancel all queued/active jobs (requires confirmation header):

```bash
curl -s -X POST \
  -H 'X-CHD-Action-Confirm: cancel-all-jobs' \
  http://localhost:8080/api/jobs/cancel-all
```

- Clear completed/failed/cancelled jobs (requires confirmation header):

```bash
curl -s -X DELETE \
  -H 'X-CHD-Action-Confirm: clear-completed-jobs' \
  http://localhost:8080/api/jobs/completed
```

- Start metadata scan + poll status:

```bash
curl -s -X POST http://localhost:8080/api/chd-metadata/scan
curl -s http://localhost:8080/api/chd-metadata/scan/status
```

### 5. Version + Release Workflow

- `.version` is the source of truth for app + image tagging.
- Sync version across files:

```bash
cd /Users/talor/github/projects/compressatorium
./scripts/sync-version.sh 3.0.2
```

- After syncing, review:
  - `/Users/talor/github/projects/compressatorium/.version`
  - `/Users/talor/github/projects/compressatorium/package.json`
  - `/Users/talor/github/projects/compressatorium/package-lock.json`
  - `/Users/talor/github/projects/compressatorium/RELEASE_NOTES.md`

### 6. GitHub Actions Workflow (`docker-image.yml`)

- Workflow name: `Build and Push Docker Image`
- Triggers: push to `main`/`latest`, tags `v*.*.*`, PRs to `main`/`latest`, manual dispatch.
- Manual dispatch example:

```bash
cd /Users/talor/github/projects/compressatorium
gh workflow run docker-image.yml --ref main -f push=true -f platforms='linux/amd64,linux/arm64'
```

- Monitor the latest run:

```bash
cd /Users/talor/github/projects/compressatorium
gh run list --workflow docker-image.yml --limit 5
gh run watch "$(gh run list --workflow docker-image.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

## Command Notes

- Preferred env names are `COMPRESSATORIUM_MOUNT_ROOT` and `COMPRESSATORIUM_VOLUMES`.
- Legacy aliases `CHD_MOUNT_ROOT` and `CHD_VOLUMES` are still supported.
- Keep default `MAX_CONCURRENT_JOBS=1` unless host capacity has been explicitly validated.
