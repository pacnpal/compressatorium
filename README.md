# Docker CHD "Compressed Hunks of Data" Converter

Compresses GDI, ISO, BIN and CUE files to CHD using **CHDMAN** from MAME Tools.

* **Web UI** for easy file browsing and conversion
* Supports **nested directories** and **compressed archives** (ZIP, 7z, RAR)
* **Multiple volume mounts** for organizing different game libraries
* Skips existing `.chd` files
* Does not delete or modify source files
* Choose between `createcd` (default) or `createdvd` modes

---

## Installation

The Docker image is available from two registries:

### Docker Hub

```bash
docker pull marctv/chd-converter
```

### GitHub Container Registry

```bash
docker pull ghcr.io/pacnpal/docker-chd-converter-webui
```

Both registries provide identical images with multi-architecture support (`linux/amd64` and `linux/arm64`).

> **Note:** In all examples below, you can substitute `marctv/chd-converter` with `ghcr.io/pacnpal/docker-chd-converter-webui` interchangeably.

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
  -v /path/to/games:/data/games \
  marctv/chd-converter
```

Then open **http://localhost:8080** in your browser.

### Multiple Volumes

Mount multiple game directories for better organization:

```bash
docker run -d \
  -p 8080:8080 \
  -e CHD_VOLUMES="/data/dreamcast,/data/psp,/data/ps1" \
  -v /home/user/dreamcast:/data/dreamcast \
  -v /home/user/psp:/data/psp \
  -v /home/user/ps1:/data/ps1 \
  marctv/chd-converter
```

### Custom Output Directory

In the Web UI, you can specify a custom output directory for converted CHD files instead of placing them alongside the source files. The directory will be created automatically as long as it is within your configured volumes.

### Features

- **File Browser**: Navigate through mounted volumes and subdirectories
- **Archive Support**: View and convert files inside ZIP, 7z, and RAR archives
- **Batch Conversion**: Select multiple files and convert them all at once
- **Progress Tracking**: Real-time progress updates via Server-Sent Events
- **CHD Inspector**: View detailed information about existing CHD files
- **Mode Selection**: Choose between CD mode (Dreamcast, PS1, etc.) or DVD mode (PSP, PS2, etc.)

---

## CLI Mode (Batch Processing)

For automated/headless conversion, use CLI mode:

### CD Conversion (Default)

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -v "$(pwd)/isofiles:/data/games:rw" \
  marctv/chd-converter
```

### DVD Conversion (PSP, PS2)

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -e CHDMAN_MODE=createdvd \
  -v "$(pwd)/isofiles:/data/games:rw" \
  marctv/chd-converter
```

### Multiple Volumes in CLI Mode

```bash
docker run --rm \
  -e CHD_MODE=cli \
  -e CHDMAN_MODE=createdvd \
  -e CHD_VOLUMES="/data/psp,/data/ps2" \
  -v /home/user/psp:/data/psp:rw \
  -v /home/user/ps2:/data/ps2:rw \
  marctv/chd-converter
```

---

## Check Existing CHD Files

Using the chdman info command directly:

```bash
docker run --rm \
  -v "/path/to/games:/data/games:ro" \
  --entrypoint chdman \
  marctv/chd-converter \
  info -i "/data/games/game.chd"
```

Or use the Web UI's CHD Inspector feature by clicking on any `.chd` file.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHD_MODE` | `webui` | Mode: `webui` (web interface) or `cli` (batch processing) |
| `CHD_VOLUMES` | `/data/games` | Comma-separated list of volume mount paths |
| `CHDMAN_MODE` | `createcd` | Conversion mode: `createcd` or `createdvd` |
| `MAX_CONCURRENT_JOBS` | `2` | Maximum parallel conversion jobs (Web UI only) |

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
    image: marctv/chd-converter
    ports:
      - "8080:8080"
    environment:
      - CHD_VOLUMES=/data/dreamcast,/data/psp,/data/ps1
      - MAX_CONCURRENT_JOBS=2
    volumes:
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
