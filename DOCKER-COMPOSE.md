# Docker Compose Quick Reference

This is a quick reference for the available Docker Compose configurations.

## Available Configurations

### 1. Single Volume Setup (Basic)
**File:** `docker-compose.yml`

Start with a single game directory:
```bash
docker-compose up -d
```

**Default configuration:**
- Port: 8080
- Volume: `./games` → `/data/games`
- Temp: `/config/temp` (inside `./config`)
- Mode: Web UI
- Concurrent jobs: 1

**Use case:** Perfect for a top-level directory containing games organized in subdirectories. The Web UI will recursively browse all subdirectories, allowing you to navigate and convert files anywhere in the directory tree.

---

### 2. Multiple Volumes (Advanced)
**File:** `docker-compose.multi-volume.yml`

For organizing different game libraries as separate mount points:
```bash
docker-compose -f docker-compose.multi-volume.yml up -d
```

**Configured volumes:**
- `./games/dreamcast` → `/data/dreamcast`
- `./games/psp` → `/data/psp`
- `./games/ps1` → `/data/ps1`
- `./games/ps2` → `/data/ps2`
- Temp: `/config/temp` (inside `./config`)

**Use case:** Ideal when you have games stored in completely separate directories (e.g., different physical drives or network shares). Each mount point appears as a separate volume in the Web UI.

**Customization:**
Edit the file to add/remove volume mounts under `/data/*`.
Use `COMPRESSATORIUM_VOLUMES` only when you want an explicit comma-separated list and to skip startup scanning.

---

### 3. CLI Mode (Batch Processing)
**File:** `docker-compose.cli.yml`

For automated/headless conversion:
```bash
docker-compose -f docker-compose.cli.yml up
```

**Behavior:**
- Converts top-level `.gdi`, `.iso`, `.cue` files in mounted volumes
- Exits after completion (no restart)
- No web interface
- CHDMAN-only (no Dolphin modes in CLI)

**Note:** CLI mode only processes files in the top level of each mounted volume. For files in subdirectories, use the Web UI mode which supports recursive directory browsing.

**To change conversion mode:**
Edit `CHDMAN_MODE` in the file:
- `createcd` for CD-ROM (Dreamcast, PS1, etc.)
- `createdvd` for DVD-ROM (PSP, PS2, etc.)

---

## Common Commands

### Start service (detached)
```bash
docker-compose up -d
```

### Stop service
```bash
docker-compose down
```

### View logs
```bash
docker-compose logs -f
```

### Check status
```bash
docker-compose ps
```

### Restart service
```bash
docker-compose restart
```

### Remove containers and volumes
```bash
docker-compose down -v
```

---

## Access Web UI

After starting with `docker-compose up -d`:
- **URL:** http://localhost:8080
- **Health Check:** http://localhost:8080/health
- **API Docs:** http://localhost:8080/docs

---

## Environment Variables

All configurations support these environment variables (edit in the compose file):

Volume behavior:
- If `COMPRESSATORIUM_VOLUMES` is set, that explicit list is used.
- If `COMPRESSATORIUM_VOLUMES` is unset, the app scans `COMPRESSATORIUM_MOUNT_ROOT/*` at startup (restart after mount changes).

| Variable | Default | Description |
|----------|---------|-------------|
| `CHD_MODE` | `webui` | Mode: `webui` or `cli` |
| `COMPRESSATORIUM_MOUNT_ROOT` | `/data` | Startup scan root for auto-discovered volumes (`/data/*`) |
| `COMPRESSATORIUM_VOLUMES` | (unset) | Explicit comma-separated volume paths (skips startup scan) |
| `CHD_MOUNT_ROOT` | `/data` | Legacy alias for `COMPRESSATORIUM_MOUNT_ROOT` |
| `CHD_VOLUMES` | (unset) | Legacy alias for `COMPRESSATORIUM_VOLUMES` |
| `CHD_DATA_DIR` | `/config` | Persistent data directory |
| `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` | `true` | Web UI: when true, `Search All` conversions return to the previous file-list view after queueing |
| `CHD_SEARCH_AUTO_RETURN_TO_FILE_LIST` | `true` | Legacy alias for `COMPRESSATORIUM_SEARCH_AUTO_RETURN_TO_FILE_LIST` |
| `CHD_TEMP_DIR` | `/config/temp` | Temporary working directory for archive extraction (auto-created) |
| `CHD_CONCURRENCY_LOCK_DIR` | `/tmp/chd-locks` | Directory for job lock files (ephemeral, auto-cleaned on container restart) |
| `COMPRESSATORIUM_DB_PATH` | `/config/compressatorium.db` | Unified SQLite database (DAT index, DAT-sync state, match cache, CHD metadata, verification state). Legacy JSON files at the paths below are auto-migrated to this DB on first startup and renamed to `*.migrated.bak` (never deleted). |
| `CHD_METADATA_STORE` | *(deprecated)* | Legacy JSON path; auto-migrated to SQLite on first startup |
| `CHD_VERIFICATION_STORE` | *(deprecated)* | Legacy JSON path; auto-migrated to SQLite on first startup |
| `CHDMAN_MODE` | `createcd` | Conversion mode: `createcd` or `createdvd` (CLI mode) |
| `CHDMAN_PATH` | `/usr/bin/chdman` | Path to chdman binary |
| `DOLPHIN_TOOL_PATH` | `/usr/local/bin/dolphin-tool` | Path to dolphin-tool binary |
| `MAX_CONCURRENT_JOBS` | `1` | Parallel conversion jobs |
| `MAX_JOB_HISTORY` | `500` | Completed jobs to retain in history |
| `CHD_CHDMAN_NICE` | `10` | Nice level for chdman (0-19) |
| `CHD_CHDMAN_IOPRIO_CLASS` | `2` | I/O priority class (`1` realtime, `2` best-effort, `3` idle) |
| `CHD_CHDMAN_IOPRIO_LEVEL` | `6` | I/O priority level (`0` highest, `7` lowest) |
| `CHD_ARCHIVE_MAX_ENTRIES` | `5000` | Max archive members to list (0 disables limit) |
| `CHD_ARCHIVE_MAX_MEMBER_SIZE` | `0` | Max size in bytes per archive member (0 disables limit) |
| `CHD_ARCHIVE_MAX_TOTAL_SIZE` | `0` | Max total size in bytes for archive listings/extractions (0 disables) |
| `CHD_INFO_TIMEOUT` | `60` | Timeout in seconds for `chdman info` (0 disables) |
| `CHD_VERIFY_TIMEOUT` | `0` | Timeout in seconds for `chdman verify` (0 disables) |
| `CHD_VERIFY_PROGRESS_TIMEOUT` | `0` | Timeout in seconds without verify output (0 disables) |
| `LOGLEVEL` | `INFO` | Log verbosity level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `LOG_PATH` | (none) | Path to log file (stdout only if unset) |
| `CHD_DEBUG_HEARTBEAT` | `30` | Maintenance loop interval (seconds) |
| `CHD_DEBUG_PROGRESS_INTERVAL` | `30` | Debug progress log interval |
| `CHD_DEBUG_PROGRESS_TIMEOUT` | `300` | Debug progress timeout |
| `CHD_PROGRESS_TIMEOUT` | `600` | Fail a conversion if progress and output size do not advance for this many seconds (0 disables) |
| `CHD_PROGRESS_TIMEOUT_PER_GIB` | `120` | Additional stall-timeout seconds per GiB of input size |
| `CHD_PROGRESS_TIMEOUT_CAP` | `7200` | Upper bound for adaptive conversion stall timeout (0 disables cap) |
| `STATIC_DIR` | `/static` | Path to static web assets |

---

## Resource Limits

Each configuration includes conservative resource limits. Adjust CPU and memory values based on your system.

### Tuning and Host Recommendations

**How to change settings**
- Edit the compose file and update `MAX_CONCURRENT_JOBS`, `CHD_CHDMAN_*`, and the `deploy.resources` limits.

**Recommended starting points**
- **Low/medium hosts (≤16 GB RAM, HDD or parity-backed arrays):** keep `MAX_CONCURRENT_JOBS=1`, `CHD_CHDMAN_NICE=10`, `CHD_CHDMAN_IOPRIO_CLASS=2`, `CHD_CHDMAN_IOPRIO_LEVEL=6`. Set a container memory limit (8–12 GB).
- **Faster hosts (32+ GB RAM, SSD cache):** try `MAX_CONCURRENT_JOBS=2` and a higher memory limit (16–24 GB). Raise I/O priority only if the host remains responsive.
- **If the host becomes sluggish:** lower `MAX_CONCURRENT_JOBS`, increase `CHD_CHDMAN_NICE`, or set `CHD_CHDMAN_IOPRIO_CLASS=3` (idle) with `CHD_CHDMAN_IOPRIO_LEVEL=7`.

**Docker host tips**
- Prefer SSD/cache for `CHD_TEMP_DIR` and CHD output to reduce array contention.
- Avoid running other heavy services during conversion.
- Always set container CPU/memory limits on shared hosts.

Example:
```yaml
deploy:
  resources:
    limits:
      cpus: '2.0'      # Maximum 2 CPU cores
      memory: 8G       # Maximum 8GB RAM
    reservations:
      cpus: '0.5'      # Reserved 0.5 CPU cores
      memory: 512M     # Reserved 512MB RAM
```

---

## Troubleshooting

### Container won't start
```bash
docker-compose logs
```

### Check health status
```bash
docker-compose ps
# Look for "healthy" status
```

### Reset everything
```bash
docker-compose down -v
docker-compose up -d
```

### Access container shell
```bash
docker-compose exec compressatorium bash
```

---

## Production Deployment

For production deployment guidance, security recommendations, and comprehensive checklists, see **[DEPLOYMENT.md](DEPLOYMENT.md)**.

Key recommendations:
- Enable resource limits
- Set up HTTPS if exposing externally
- Consider adding authentication
- Monitor disk space and resource usage
- Regular backups of converted files
