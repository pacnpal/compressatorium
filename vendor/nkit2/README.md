# NKit2 Binaries

NKit2 is required for Redump-compatible RVZ output (GameCube/Wii).

## Download

Download NKit2 from: https://gbatemp.net/download/nkit.36157/

## Setup

Place the **self-contained** (standalone) Linux builds in the appropriate directories:

- `linux-amd64/` - x86_64 Linux build
- `linux-arm64/` - ARM64 Linux build

The directory should contain the `nkit` binary and any supporting files from the NKit2 distribution.

## Verification

After placing files, the Dockerfile will automatically COPY the correct architecture's
binary to `/opt/nkit/` in the container image.

## Version

MAME Redump uses NKit2 RVZ with settings: `rvz:zstd:19:128k`
Source: https://github.com/MetalSlug/MAMERedump
