# Compressatorium

> **Fork Notice:** This project is a fork of [MarcTV/docker-chd-converter](https://github.com/MarcTV/docker-chd-converter) with an added Web UI and additional features. Thanks to [MarcTV](https://github.com/MarcTV) for the original CLI-based converter!

Multi-tool game disc image converter supporting **CHDMAN** (MAME) and **dolphin-tool** (Dolphin Emulator).

* **Web UI** for easy file browsing and conversion
* Supports **nested directories** and **compressed archives** (ZIP, 7z, RAR)
* **Multiple volume mounts** for organizing different game libraries
* Skips existing `.chd` files
* Source files are preserved by default (optional delete-on-verify after successful conversion)
* Choose between `createcd` (default) or `createdvd` modes

---

## Installation

The Docker image is available from two registries:

### Docker Hub

```bash
docker pull pacnpal/chd-converter
```

### GitHub Container Registry

```bash
docker pull ghcr.io/pacnpal/docker-chd-converter-webui
```

Both registries provide identical images with multi-architecture support (`linux/amd64` and `linux/arm64`).

> **Note:** In all examples below, you can substitute `pacnpal/chd-converter` with `ghcr.io/pacnpal/docker-chd-converter-webui` interchangeably.

### Available Tags

| Tag | Description |
|-----|-------------|
| `latest` | Latest stable release from the main branch |
| `vX.Y.Z` | Specific version (e.g., `v1.0.0`) |
| `sha-xxxxxxx` | Specific commit build |

---

## Web UI Mode (Default)

The easiest way to use CHD Converter is through the web interface:

```bash
docker run -d \
  -p 8080:8080 \
  -v /path/to/config:/config \
  -v /path/to/games:/data/games \
  pacnpal/chd-converter
```

Then open **http://localhost:8080** in your browser.

> **Required:** The `/config` volume must be mounted for persistent data storage.  
> **Default temp location:** `/config/temp`. To use a different location, set `CHD_TEMP_DIR` and mount it.

### Multiple Volumes

Mount multiple game directories for better organization:

```bash
docker run -d \
  -p 8080:8080 \
  -v /path/to/config:/config \
  -e CHD_VOLUMES="/data/dreamcast,/data/psp,/data/ps1" \
  -v /home/user/dreamcast:/data/dreamcast \
  -v /home/user/psp:/data/psp \
  -v /home/user/ps1:/data/ps1 \
  pacnpal/chd-converter
```

### Custom Output Directory

In the Web UI, you can specify a custom output directory for converted CHD files instead of placing them alongside the source files. The directory will be created automatically as long as it is within your configured volumes.

### Features

**File Browser**
- Navigate through mounted volumes and subdirectories
- View file sizes, types, and CHD status indicators
- Recursive search to find all convertible files across the entire volume

**Archive Support**
- Browse inside ZIP, 7z, and RAR archives without extraction
- Convert files directly from within archives
- Archives extract temporarily during conversion, then clean up automatically
- When a `.cue`/`.gdi` is present in the same archive folder, `.bin` entries are suppressed and batch jobs are deduplicated by output path to avoid stalled conversions.
- Archive listings include safety limits (max entries/size) and expose truncation metadata when limits are hit.

**Batch Conversion**
- Select multiple files and convert them all at once
- Queue-based processing (FIFO) with configurable concurrency
- Real-time progress tracking via Server-Sent Events
- Duplicate detection with options to skip, rename, or overwrite
- Optional delete-on-verify with a preflight confirmation list (includes `.cue`/`.gdi` track files)
- Archive conversions can delete the entire archive after verify (explicit warning in the delete plan)

**Bulk Operations**
- **Bulk Delete**: Delete multiple selected files at once
- **Bulk Verify**: Verify integrity of multiple CHD files simultaneously
- Smart categorization showing source files with/without CHD backups
- Warnings for files without verified CHD backups before deletion

**CHD Verification**
- Verify integrity of CHD files using chdman's built-in verification
- Verification status persisted across sessions (stored in `/config/verified_chds.json`)
- Integrated verification workflow when deleting source files
- Visual indicators showing verified vs unverified CHD files
- Optional timeouts for long-running verifications and stalled progress

**CHD Inspector**
- View detailed CHD file information (version, compression, size, hashes)
- SHA1 and Data SHA1 checksums displayed
- Raw chdman output available for advanced inspection

**File Management**
- Rename files and directories
- Delete files with safety checks (warns about missing CHD backups)
- Empty directory cleanup

**Conversion Modes**
- **Create CHD**: createcd (CD), createdvd (DVD/PSP/PS2), createraw, createhd, createld
- **Extract from CHD**: extractcd, extractdvd, extractraw, extracthd, extractld
- **Copy/Recompress**: Recompress existing CHD files with different codecs

**Compression Options**
- Choose from multiple compression codecs: zlib, zstd, lzma, huff, flac
- CD-specific codecs: cdzl, cdzs, cdlz, cdfl
- No compression option for maximum compatibility
- Select up to 4 codecs per conversion

---

## CLI Mode (Batch Processing)

For automated/headless conversion, use CLI mode:

### CD Conversion (Default)

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -v "$(pwd)/isofiles:/data/games:rw" \
  pacnpal/chd-converter
```

### DVD Conversion (PSP, PS2)

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -e CHDMAN_MODE=createdvd \
  -v "$(pwd)/isofiles:/data/games:rw" \
  pacnpal/chd-converter
```

### Multiple Volumes in CLI Mode

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -e CHDMAN_MODE=createdvd \
  -e CHD_VOLUMES="/data/psp,/data/ps2" \
  -v /home/user/psp:/data/psp:rw \
  -v /home/user/ps2:/data/ps2:rw \
  pacnpal/chd-converter
```

---

## Check Existing CHD Files

Using the chdman info command directly:

```bash
docker run --rm \
  -v "/path/to/games:/data/games:ro" \
  --entrypoint chdman \
  pacnpal/chd-converter \
  info -i "/data/games/game.chd"
```

Or use the Web UI's CHD Inspector feature by clicking on any `.chd` file.

---

## Compression Presets (Emulator Compatibility)

Some emulators (notably NetherSX2/AetherSX2) only support **zlib**-compressed CHDs. In the Web UI, choose:

- **Compression: Default** (uses chdman defaults)
- **NetherSX2/AetherSX2 (zlib only)** to force `zlib`
- **Custom compression list** (comma-separated) for advanced users (passed directly to `chdman -c`)

If you see emulator errors like “Failed to initialize cdvd,” re-convert with the zlib-only preset.
Use `chdman help createcd` or `chdman help createdvd` to see the expected `-c` format for your version.

---

## Supported Operations

All actions are queued and processed by the job queue (FIFO). The queue is the only execution path.

**Create CHD**
- `createraw`, `createhd`, `createcd`, `createdvd`, `createld`

**Extract from CHD**
- `extractraw`, `extracthd`, `extractcd`, `extractdvd`, `extractld`

**Copy / Recompress**
- `copy` (CHD → CHD, optionally with new compression)

Notes:
- Compression applies to **create** and **copy** operations only.
- Extract operations ignore compression settings.
- `extractcd` produces both `.cue` and `.bin` outputs.

---

## API Endpoints

The Web UI communicates with a REST API that can also be used directly. Interactive API documentation is available at `/docs` when running the container.

### File Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/volumes` | List configured volume mount points |
| GET | `/api/files` | List files in a directory |
| GET | `/api/files/search` | Recursively search for convertible files |
| GET | `/api/files/archive` | List contents of an archive file |
| POST | `/api/files/rename` | Rename a file or directory |
| DELETE | `/api/files/delete` | Delete a single file or empty directory |
| POST | `/api/files/delete-batch` | Delete multiple files at once |

### Conversion Jobs

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/jobs` | Create a single conversion job |
| POST | `/api/jobs/batch` | Create multiple conversion jobs |
| POST | `/api/jobs/check-duplicates` | Check for existing output files |
| POST | `/api/jobs/delete-plan` | Build delete-on-verify confirmation list |
| GET | `/api/jobs` | List all jobs |
| GET | `/api/jobs/{id}` | Get a specific job |
| DELETE | `/api/jobs/{id}` | Cancel a job |
| DELETE | `/api/jobs/completed` | Clear completed/failed/cancelled jobs |
| GET | `/api/jobs/events` | SSE stream for job progress updates |

### CHD Information & Verification

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/info` | Get CHD file metadata |
| GET | `/api/verify` | Verify a CHD file's integrity |
| GET | `/api/verify/events` | SSE stream for verification progress |
| POST | `/api/verify-batch/events` | SSE stream for batch verification |
| GET | `/api/verified` | List all verified CHD paths |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHD_MODE` | `webui` | Mode: `webui` (web interface) or `cli` (batch processing) |
| `CHD_VOLUMES` | `/data/games` | Comma-separated list of volume mount paths |
| `CHD_DATA_DIR` | `/config` | Directory for persistent application data |
| `CHD_TEMP_DIR` | `/config/temp` | Temporary working directory for archive extraction |
| `CHDMAN_MODE` | `createcd` | Conversion mode: `createcd` or `createdvd` (CLI mode only) |
| `CHDMAN_PATH` | `/usr/bin/chdman` | Path to chdman binary (for custom builds) |
| `MAX_CONCURRENT_JOBS` | `1` | Maximum parallel conversion jobs |
| `MAX_JOB_HISTORY` | `500` | Maximum completed jobs to retain in history |
| `CHD_CHDMAN_NICE` | `10` | Nice level for chdman (0-19, higher = lower priority) |
| `CHD_CHDMAN_IOPRIO_CLASS` | `2` | I/O priority class (`1` realtime, `2` best-effort, `3` idle) |
| `CHD_CHDMAN_IOPRIO_LEVEL` | `6` | I/O priority level (`0` highest, `7` lowest) |
| `CHD_ARCHIVE_MAX_ENTRIES` | `5000` | Max archive members to list (0 disables limit) |
| `CHD_ARCHIVE_MAX_MEMBER_SIZE` | `0` | Max size in bytes per archive member (0 disables limit) |
| `CHD_ARCHIVE_MAX_TOTAL_SIZE` | `0` | Max total size in bytes for archive listings/extractions (0 disables limit) |
| `CHD_INFO_TIMEOUT` | `60` | Timeout in seconds for `chdman info` (0 disables) |
| `CHD_VERIFY_TIMEOUT` | `0` | Timeout in seconds for `chdman verify` (0 disables) |
| `CHD_VERIFY_PROGRESS_TIMEOUT` | `0` | Timeout in seconds without verify output (0 disables) |
| `CHD_DEBUG` | `false` | Enable debug logging |
| `CHD_DEBUG_LOG_PATH` | (none) | Path to debug log file |
| `CHD_DEBUG_HEARTBEAT` | `30` | Debug heartbeat interval in seconds |
| `CHD_DEBUG_PROGRESS_INTERVAL` | `30` | Debug progress log interval in seconds |
| `CHD_DEBUG_PROGRESS_TIMEOUT` | `300` | Debug progress timeout in seconds |
| `CHD_PROGRESS_TIMEOUT` | `600` | Fail a conversion if progress and output size do not advance for this many seconds (0 disables) |

Defaults are intentionally conservative to reduce host impact during conversion. Increase `MAX_CONCURRENT_JOBS` or adjust `CHD_CHDMAN_*` only if your host has ample CPU/RAM and fast storage. By default temp files go to `/config/temp`; set `CHD_TEMP_DIR` to use a faster disk and mount it into the container.

---

## Persistent Data

The `/config` volume is **required** and must be mounted for the application to store persistent data.

```bash
-v /path/to/config:/config
```

### Data Files

| File | Location | Description |
|------|----------|-------------|
| `verified_chds.json` | `/config/` | Records of verified CHD files (integrity checks) |

---

## Docker Compose

The repository includes ready-to-use Docker Compose configurations:

- **`docker-compose.yml`** - Single volume setup with subdirectory support
- **`docker-compose.multi-volume.yml`** - Multiple separate volume mounts
- **`docker-compose.cli.yml`** - CLI/batch processing mode

### Quick Start

1. **Single Volume Setup** (recommended for most users):
   - Mount a top-level directory containing your games in subdirectories
   - The Web UI will recursively browse all subdirectories
   
```bash
docker-compose up -d
```

The default compose files include conservative CPU/memory limits to help avoid host lockups during large conversions. Adjust those limits to match your system.

### Tuning and Host Recommendations

**How to change settings**
- **Docker Compose:** edit `docker-compose.yml` (or `docker-compose.multi-volume.yml`) and update `MAX_CONCURRENT_JOBS`, `CHD_CHDMAN_*`, and the `deploy.resources` limits.
- **Docker run / Unraid:** set environment variables in the container template and apply CPU/memory limits there.

**Recommended starting points**
- **Low/medium hosts (≤16 GB RAM, HDD or parity-backed arrays):** keep `MAX_CONCURRENT_JOBS=1`, `CHD_CHDMAN_NICE=10`, `CHD_CHDMAN_IOPRIO_CLASS=2`, `CHD_CHDMAN_IOPRIO_LEVEL=6`. Set a container memory limit (8–12 GB).
- **Faster hosts (32+ GB RAM, SSD cache):** try `MAX_CONCURRENT_JOBS=2` and a higher memory limit (16–24 GB). Raise I/O priority only if the host remains responsive.
- **If the host becomes sluggish:** lower `MAX_CONCURRENT_JOBS`, increase `CHD_CHDMAN_NICE`, or set `CHD_CHDMAN_IOPRIO_CLASS=3` (idle) with `CHD_CHDMAN_IOPRIO_LEVEL=7`.

**Docker host tips**
- Prefer SSD/cache for `CHD_TEMP_DIR` and CHD output to reduce array contention.
- Avoid running other heavy services during conversion.
- Always set container CPU/memory limits on shared hosts.

2. **Multiple Volumes:**
```bash
docker-compose -f docker-compose.multi-volume.yml up -d
```

3. **CLI Batch Processing:**
```bash
docker-compose -f docker-compose.cli.yml up
```

### Example Configuration

```yaml
version: '3.8'

services:
  chd-converter:
    image: pacnpal/chd-converter
    ports:
      - "8080:8080"
    environment:
      - CHD_VOLUMES=/data/dreamcast,/data/psp,/data/ps1
      - MAX_CONCURRENT_JOBS=1
      - CHD_CHDMAN_NICE=10
      - CHD_CHDMAN_IOPRIO_CLASS=2
      - CHD_CHDMAN_IOPRIO_LEVEL=6
    volumes:
      - /home/user/chd-converter-config:/config
      - /home/user/games/dreamcast:/data/dreamcast
      - /home/user/games/psp:/data/psp
      - /home/user/games/ps1:/data/ps1
    restart: unless-stopped
```

For production deployment guidance, see [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Supported File Types

**Input formats:**
- `.gdi` - GD-ROM (Dreamcast)
- `.iso` - ISO 9660 disc images
- `.cue` / `.bin` - CD images with cue sheets

**Archive formats (Web UI):**
- `.zip` - ZIP archives
- `.7z` - 7-Zip archives
- `.rar` - RAR archives

**Output format:**
- `.chd` - Compressed Hunks of Data

---

## Acknowledgments

This project is a fork of the original [docker-chd-converter](https://github.com/MarcTV/docker-chd-converter) by [MarcTV](https://github.com/MarcTV). The original project provides a simple CLI-based batch converter, and this fork extends it with a Web UI and additional features.

**Original Project:**
- Repository: [github.com/MarcTV/docker-chd-converter](https://github.com/MarcTV/docker-chd-converter)
- Docker Hub: [hub.docker.com/r/marctv/chd-converter](https://hub.docker.com/r/marctv/chd-converter)

Thank you MarcTV for creating and sharing the original converter!
