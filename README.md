# Compressatorium

> **Fork Notice:** This project is a fork of MarcTV's original Docker CHD Converter project with an added Web UI and additional features. Thanks to [MarcTV](https://github.com/MarcTV) for the original CLI-based converter!

Multi-tool game disc image converter supporting **CHDMAN** (MAME) and **dolphin-tool** (Dolphin Emulator).

* **Web UI** for easy file browsing and conversion
* Supports **nested directories** and **compressed archives** (ZIP, 7z, RAR)
* **Multiple volume mounts** for organizing different game libraries
* **ISO handling toggle** (defaults to Dolphin tool for `.iso`; switch to CHDMAN)
* Web UI detects existing outputs with skip/rename/overwrite options
* CLI skips existing CHD files by default
* Source files are preserved by default (optional delete-on-verify after successful conversion)
* Supports CHD create/extract/copy plus Dolphin RVZ/WIA/GCZ/ISO conversions (Web UI/API)

---

## Installation

The Docker image is available from two registries:

### Docker Hub

```bash
docker pull pacnpal/compressatorium
```

### GitHub Container Registry

```bash
docker pull ghcr.io/pacnpal/compressatorium
```

Both registries provide identical images with multi-architecture support (`linux/amd64` and `linux/arm64`).

> **Note:** Use either registry: replace `pacnpal/compressatorium` with `ghcr.io/pacnpal/compressatorium` for the same image.

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
  pacnpal/compressatorium
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
  pacnpal/compressatorium
```

### Custom Output Directory

In the Web UI, you can specify a custom output directory for converted CHD or Dolphin disc images instead of placing them alongside the source files. The directory will be created automatically as long as it is within your configured volumes.

### Features

**File Browser**
- Navigate through mounted volumes and subdirectories
- View file sizes, types, ISO handling, and CHD status indicators
- Recursive search to find all convertible files across the entire volume

**Archive Support**
- Browse inside ZIP, 7z, and RAR archives without extraction
- Convert files directly from within archives
- Archives extract temporarily during conversion, then clean up automatically
- When a `.cue`/`.gdi` is present in the same archive folder, `.bin` entries are suppressed and batch jobs are deduplicated by output path to avoid stalled conversions.
- Archive listings include safety limits (max entries/size) and expose truncation metadata when limits are hit.
- Archive inputs are limited to CHD create modes (not extract/copy/Dolphin).

**ISO Handling & Dolphin Tools (GameCube/Wii)**
- Toggle ISO handling between CHDMAN and Dolphin (controls ISO info/verify and conversions)
- Convert `.iso`, `.gcz`, `.wia`, `.rvz`, `.wbfs` with dolphin-tool (RVZ/WIA/GCZ/ISO output)
- Disc info and verification for Dolphin formats (including batch verification)
- Dolphin modes require direct disc images (archive members are not supported)

**Batch Conversion**
- Select multiple files and convert them all at once
- Queue-based processing (FIFO) with configurable concurrency
- Real-time progress tracking via Server-Sent Events
- Duplicate detection with options to skip, rename, or overwrite
- Optional delete-on-verify with a preflight confirmation list (includes `.cue`/`.gdi` track files)
- Archive conversions can delete the entire archive after verify (explicit warning in the delete plan)

**Bulk Operations**
- **Bulk Delete**: Delete multiple selected files at once
- **Bulk Verify**: Verify integrity of multiple CHD + Dolphin images simultaneously
- Smart categorization showing source files with/without CHD backups
- Warnings for files without verified CHD backups before deletion

**Verification**
- Verify CHD files using chdman's built-in verification
- Verify GameCube/Wii disc images using dolphin-tool (ISO uses Dolphin when ISO handling is set to Dolphin)
- Verification status persisted across sessions (stored in `/config/verified_chds.json`)
- Integrated verification workflow when deleting source files
- Visual indicators showing verified vs unverified items
- Optional timeouts for long-running verifications and stalled progress

**CHD Inspector**
- View detailed CHD file information (version, compression, size, hashes)
- SHA1 and Data SHA1 checksums displayed
- Raw chdman output available for advanced inspection
- Dolphin disc info shows game ID, region, format, compression, and raw output

**CHD Metadata Cache**
- Background metadata scan with CD/DVD badges
- "Scan Metadata" and "Force Rescan" actions to refresh cached metadata
- Cache stored in `/config/chd_metadata.json`

**File Management**
- Rename files and directories
- Delete files with safety checks (warns about missing CHD backups)
- Empty directory cleanup

**Conversion Modes**
- **Create CHD**: createcd (CD), createdvd (DVD/PSP/PS2), createraw, createhd, createld
- **Extract from CHD**: extractcd, extractdvd, extractraw, extracthd, extractld
- **Copy/Recompress**: Recompress existing CHD files with different codecs
- **Dolphin (GameCube/Wii)**: dolphin_rvz, dolphin_wia, dolphin_gcz, dolphin_iso

**Compression Options**
- Choose from multiple compression codecs: zlib, zstd, lzma, huff, flac, avhu (A/V Huffman)
- CD-specific codecs: cdzl, cdzs, cdlz, cdfl (CD images only)
- No compression option for maximum compatibility (`-c none`)
- Select up to 4 codecs per conversion (CHD only)
- Dolphin modes accept one codec + optional level (RVZ/WIA), while GCZ/ISO ignore compression

---

## Dolphin Emulator Support (GameCube/Wii)

Dolphin support is available in the Web UI and REST API (CLI mode remains CHDMAN-only).

**Supported inputs:** `.iso`, `.gcz`, `.wia`, `.rvz`, `.wbfs`  
**Output modes:** `dolphin_rvz` (recommended), `dolphin_wia`, `dolphin_gcz`, `dolphin_iso`

**Notes**
- Requires the Docker image with Dolphin installed (default image includes `dolphin-emu` + wrapper).
- Dolphin conversions use `dolphin-tool` (configurable via `DOLPHIN_TOOL_PATH`).
- Compression is a single codec with an optional level (`zstd:5`, `bzip2:5`, `lzma:5`, `lzma2:5`).
- `dolphin_gcz` uses fixed compression and ignores codec selection.
- `dolphin_iso` outputs an uncompressed ISO image.
- Archive members are **not** supported for Dolphin conversions.
- ISO info/verify and conversions follow the ISO Handling toggle in the UI (default: Dolphin for ISO files).

---

## CLI Mode (Batch Processing)

For automated/headless conversion, use CLI mode. CLI mode runs CHDMAN only and processes
files in the **top level** of each mounted volume (no recursive scanning, no archives). See
`DOCKER-COMPOSE.md` for CLI behavior details.

### CD Conversion (Default)

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -v "$(pwd)/isofiles:/data/games:rw" \
  pacnpal/compressatorium
```

### DVD Conversion (PSP, PS2)

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -e CHDMAN_MODE=createdvd \
  -v "$(pwd)/isofiles:/data/games:rw" \
  pacnpal/compressatorium
```

### Multiple Volumes in CLI Mode

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -e CHDMAN_MODE=createdvd \
  -e CHD_VOLUMES="/data/psp,/data/ps2" \
  -v /home/user/psp:/data/psp:rw \
  -v /home/user/ps2:/data/ps2:rw \
  pacnpal/compressatorium
```

---

## Check Existing CHD Files

Using the chdman info command directly:

```bash
docker run --rm \
  -v "/path/to/games:/data/games:ro" \
  --entrypoint chdman \
  pacnpal/compressatorium \
  info -i "/data/games/game.chd"
```

Or use the Web UI's CHD Inspector feature by clicking on any `.chd` file.

---

## Compression Compatibility Tips

Some emulators (notably NetherSX2/AetherSX2) only support **zlib**-compressed CHDs. If you see
errors like “Failed to initialize cdvd,” re-convert with **zlib only**.

- **zlib**: best compatibility across emulators
- **zstd**: fast + small, but older software may not support it
- **lzma**: highest compression, slowest
- **No compression**: uses `-c none` for uncompressed output
- **CD-specific codecs**: use cdzl/cdzs/cdlz/cdfl for CD images only

For Dolphin formats, choose a single codec (zstd/bzip2/lzma/lzma2) and an optional level.
Use `chdman help createcd` or `chdman help createdvd` for codec details.

---

## Supported Operations

All actions are queued and processed by the job queue (FIFO). The queue is the only execution path.

**Create CHD**
- `createraw`, `createhd`, `createcd`, `createdvd`, `createld`

**Extract from CHD**
- `extractraw`, `extracthd`, `extractcd`, `extractdvd`, `extractld`

**Copy / Recompress**
- `copy` (CHD → CHD, optionally with new compression)

**Dolphin (GameCube/Wii)**
- `dolphin_rvz`, `dolphin_wia`, `dolphin_gcz`, `dolphin_iso`

Notes:
- Compression applies to **create**/**copy** and Dolphin RVZ/WIA operations only.
- Extract operations ignore compression settings.
- `extractcd` produces both `.cue` and `.bin` outputs.
- Dolphin GCZ/ISO outputs ignore compression selection.
- Archive inputs are supported for create modes only (not extract/copy/Dolphin).

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

### CHD Metadata & Version

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/version` | Get app version |
| POST | `/api/chd-metadata` | Fetch cached CHD metadata for multiple files |
| POST | `/api/chd-metadata/scan` | Trigger background metadata scan |
| GET | `/api/chd-metadata/scan/status` | Check metadata scan status |

### Dolphin Disc Info & Verification

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/dolphin-info` | Get Dolphin disc metadata |
| GET | `/api/dolphin-verify` | Verify a disc image's integrity |
| GET | `/api/dolphin-verify/events` | SSE stream for Dolphin verification progress |
| POST | `/api/dolphin-verify-batch/events` | SSE stream for batch Dolphin verification |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHD_MODE` | `webui` | Mode: `webui` (web interface) or `cli` (batch processing) |
| `CHD_VOLUMES` | `/data/games` | Comma-separated list of volume mount paths |
| `CHD_DATA_DIR` | `/config` | Directory for persistent application data |
| `CHD_TEMP_DIR` | `/config/temp` | Temporary working directory for archive extraction (auto-created) |
| `CHD_CONCURRENCY_LOCK_DIR` | `/config/locks` | Directory for job lock files |
| `CHD_METADATA_STORE` | `/config/chd_metadata.json` | CHD metadata cache file path |
| `CHD_VERIFICATION_STORE` | `/config/verified_chds.json` | Verification store file path |
| `CHDMAN_MODE` | `createcd` | Conversion mode: `createcd` or `createdvd` (CLI mode only) |
| `CHDMAN_PATH` | `/usr/bin/chdman` | Path to chdman binary (for custom builds) |
| `DOLPHIN_TOOL_PATH` | `/usr/local/bin/dolphin-tool` | Path to dolphin-tool binary |
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
| `STATIC_DIR` | `/static` | Path to static web assets |

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
| `verified_chds.json` | `/config/` | Records of verified CHD/Dolphin files (integrity checks; filename retained for backward compatibility with existing installs) |
| `chd_metadata.json` | `/config/` | Cached CHD metadata (media type, info cache) |
| `locks/` | `/config/locks` | Job lock files for concurrency control |

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
  compressatorium:
    image: pacnpal/compressatorium
    ports:
      - "8080:8080"
    environment:
      - CHD_VOLUMES=/data/dreamcast,/data/psp,/data/ps1
      - MAX_CONCURRENT_JOBS=1
      - CHD_CHDMAN_NICE=10
      - CHD_CHDMAN_IOPRIO_CLASS=2
      - CHD_CHDMAN_IOPRIO_LEVEL=6
    volumes:
      - /home/user/compressatorium-config:/config
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
- `.iso` - ISO 9660 disc images (CHD or Dolphin based on ISO handling)
- `.cue` / `.bin` - CD images with cue sheets
- `.gcz`, `.wia`, `.rvz`, `.wbfs` - GameCube/Wii disc images (Dolphin)

**Archive formats (Web UI):**
- `.zip` - ZIP archives
- `.7z` - 7-Zip archives
- `.rar` - RAR archives

**Output format:**
- `.chd` - Compressed Hunks of Data
- `.rvz`, `.wia`, `.gcz`, `.iso` - Dolphin output formats

---

## Acknowledgments

This project is a fork of the original Docker CHD Converter project by [MarcTV](https://github.com/MarcTV). The original project provides a simple CLI-based batch converter, and this fork extends it with a Web UI and additional features.

**Original Project:**
- Author: [MarcTV](https://github.com/MarcTV)

Thank you MarcTV for creating and sharing the original converter!
