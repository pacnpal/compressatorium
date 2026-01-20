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
- Mode: Web UI
- Concurrent jobs: 2

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

**Use case:** Ideal when you have games stored in completely separate directories (e.g., different physical drives or network shares). Each mount point appears as a separate volume in the Web UI.

**Customization:**
Edit the file to add/remove volumes and update the `CHD_VOLUMES` environment variable.

---

### 3. CLI Mode (Batch Processing)
**File:** `docker-compose.cli.yml`

For automated/headless conversion:
```bash
docker-compose -f docker-compose.cli.yml up
```

**Behavior:**
- Converts all files in mounted volumes
- Exits after completion (no restart)
- No web interface

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

| Variable | Default | Description |
|----------|---------|-------------|
| `CHD_MODE` | `webui` | Mode: `webui` or `cli` |
| `CHD_VOLUMES` | `/data/games` | Comma-separated volume paths |
| `CHDMAN_MODE` | `createcd` | Conversion mode: `createcd` or `createdvd` |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel conversion jobs (Web UI only) |

---

## Resource Limits

Each configuration includes commented resource limits. To enable:

1. Uncomment the `deploy` section in the compose file
2. Adjust CPU and memory values based on your system

Example:
```yaml
deploy:
  resources:
    limits:
      cpus: '2.0'      # Maximum 2 CPU cores
      memory: 4G       # Maximum 4GB RAM
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
docker-compose exec chd-converter bash
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
