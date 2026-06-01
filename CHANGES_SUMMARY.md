# Changes summary: Docker Compose and deployment docs

This records the change that added the Docker Compose configs and the deployment docs. It's a historical summary, not current reference. For the live docs, see [DEPLOYMENT.md](DEPLOYMENT.md) and [DOCKER-COMPOSE.md](DOCKER-COMPOSE.md).

## What it added

**Compose files**
- `docker-compose.yml`: single-volume Web UI setup with a health check, a restart policy, and active resource limits.
- `docker-compose.multi-volume.yml`: four game-library mounts as separate volumes.
- `docker-compose.cli.yml`: CLI batch mode that exits after the run, with no restart policy.

**Docs**
- `DEPLOYMENT.md`: deployment guide covering security, the health check, tuning, and a checklist.
- `DOCKER-COMPOSE.md`: quick reference for the compose files, common commands, and troubleshooting.
- `README.md`: added the Docker Compose section and links to both guides.

**Build**
- `.dockerignore`: keeps the build context small.

## Security review at the time

The code review found no hardcoded secrets, path-traversal protection in `files.py` and `convert.py`, command execution through `asyncio.create_subprocess_exec()` with no shell, file paths validated against the configured volumes, and archive extraction guarded by the same validation. Those still hold. See [DEPLOYMENT.md](DEPLOYMENT.md) for the current state.

## Follow-ups since

Some items flagged here have since shipped. The runtime drops to a non-root `converter` user (`entrypoint.sh` plus `gosu`), and the compose files carry resource limits. What's still left to the operator: HTTPS and auth through a reverse proxy, security headers, rate limiting, and monitoring. DEPLOYMENT.md tracks the live list.
