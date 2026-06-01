FROM debian:trixie-slim AS builder

# Install build dependencies
#
# DL3008 (pin apt versions) is intentionally ignored: build-stage uses
# generic packages (git, build-essential, libzstd-dev) where the latest
# patched versions are preferable to a frozen pin.  Reproducibility comes
# from the snapshot-pinned mame-tools deb in the runtime stage below, not
# from these build deps.
ENV DEBIAN_FRONTEND=noninteractive
# hadolint ignore=DL3008
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libzstd-dev \
    ca-certificates && \
    git clone https://github.com/energeticokay/z3ds_compress.git /tmp/z3ds

WORKDIR /tmp/z3ds

RUN g++ -O3 src/*.cpp -o z3ds_compressor -lzstd && \
    chmod +x z3ds_compressor

# ---------------------------------------------------------------------------
# Frontend builder stage — compile the Svelte 5 SPA with Vite.
#
# Output is emitted to /build/static via vite.config.js (build.outDir),
# which the runtime stage copies into /static.  Multi-arch safe:
# node:lts-slim ships linux/amd64 and linux/arm64 manifests, and the SPA
# has no native dependencies so QEMU emulation under buildx works.
# ---------------------------------------------------------------------------
FROM node:lts-slim AS frontend-builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY vite.config.js svelte.config.js index.html ./
COPY src/ ./src/
ARG APP_VERSION=dev
ENV VITE_APP_VERSION=${APP_VERSION}
RUN npm run build

FROM debian:trixie-slim

# ---------------------------------------------------------------------------
# Immutable pin: mame-tools 0.285+dfsg1-1 from snapshot.debian.org
#
# All runtime deps (libflac14, libsdl2-2.0-0, libutf8proc3, zlib1g, etc.)
# are satisfiable from trixie-slim's own repos — no foreign sources needed.
#
# snapshot.debian.org URLs are content-addressed and timestamp-locked;
# the .deb behind a given URL will never change.
# ---------------------------------------------------------------------------
ARG TARGETARCH

ARG MAME_TOOLS_SNAPSHOT="https://snapshot.debian.org/archive/debian/20260213T023117Z/pool/main/m/mame"
ARG MAME_TOOLS_VERSION="0.285+dfsg1-1"
ARG MAME_TOOLS_SHA256_AMD64="d99e82887aab57d9a66b2f1ffd80210aabeb064808a6d05f69af1584049fd195"
ARG MAME_TOOLS_SHA256_ARM64="6388bff0f6242dfd3a09c63c6e25ab94e0a64fe7cf2b3b0170f89ff7c13340a8"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system dependencies, pinned mame-tools, create wrapper script, and prepare venv
#
# DL3008 (pin apt versions) is intentionally ignored: mame-tools is pinned via
# the snapshot.debian.org .deb downloaded inside the RUN below; the remaining
# packages (python3, util-linux, gosu, etc.) are stable trixie security-tracked
# dependencies where pinning would block routine CVE patches.
ENV DEBIAN_FRONTEND=noninteractive
# hadolint ignore=DL3008
RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      python3-venv \
      util-linux \
      unrar-free \
      p7zip-full \
      wget \
      unzip \
      zstd \
      bash \
      gosu \
      ca-certificates && \
    # Install dolphin-emu only where available/practical (non-fatal)
    if [ "$TARGETARCH" = "amd64" ]; then \
      apt-get install -y --no-install-recommends dolphin-emu || \
        echo "WARNING: dolphin-emu install failed on amd64; continuing without it"; \
    else \
      echo "Skipping dolphin-emu on ${TARGETARCH}"; \
    fi && \
    # --- Install pinned mame-tools from snapshot ---
    MAME_DEB="mame-tools_${MAME_TOOLS_VERSION}_${TARGETARCH}.deb" && \
    if [ "$TARGETARCH" = "amd64" ]; then \
      EXPECTED_SHA256="${MAME_TOOLS_SHA256_AMD64}"; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
      EXPECTED_SHA256="${MAME_TOOLS_SHA256_ARM64}"; \
    else \
      echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1; \
    fi && \
    wget -q "${MAME_TOOLS_SNAPSHOT}/${MAME_DEB}" -O /tmp/mame-tools.deb && \
    echo "${EXPECTED_SHA256}  /tmp/mame-tools.deb" | sha256sum -c - && \
    dpkg -i /tmp/mame-tools.deb || apt-get install -y -f --no-install-recommends && \
    rm /tmp/mame-tools.deb && \
    # --- Verify chdman version (capture fully to avoid SIGPIPE under pipefail) ---
    CHDMAN_VER="$(chdman 2>&1 || true)" && echo "$CHDMAN_VER" | grep -q "0\.285" && \
    # --- Clean up ---
    rm -rf /var/lib/apt/lists/* && \
    # Only create dolphin-tool wrapper if the binary exists
    if command -v /usr/games/dolphin-tool >/dev/null 2>&1; then \
      printf '#!/bin/bash\nexec /usr/games/dolphin-tool "$@"\n' > /usr/local/bin/dolphin-tool && \
      chmod +x /usr/local/bin/dolphin-tool; \
    fi && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir "pip>=25.3"

ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /app/
# This also installs `nsz` (Nintendo Switch NSP/XCI <-> NSZ/XCZ), which lands on
# PATH at /opt/venv/bin/nsz. nsz needs the operator's own prod.keys mounted at
# runtime (NSZ_KEYS_PATH); no keys are baked into this image.
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install z3ds_compressor from builder stage
COPY --from=builder /tmp/z3ds/z3ds_compressor /usr/local/bin/z3ds_compressor

# Copy application
COPY app/ /app/
# Bring in tracked static assets (images, etc.) and then overlay the
# Vite-built SPA (index.html + hashed assets/) from the frontend-builder
# stage. emptyOutDir=false in vite.config.js, plus this ordering, keeps
# /static/images alongside the generated /static/index.html + /static/assets.
COPY static/ /static/
COPY --from=frontend-builder /build/static/ /static/
COPY migrations/ /migrations/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app

# Version injected from GitHub release tag at build time
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Configuration
ENV COMPRESSATORIUM_MOUNT_ROOT="/data"
ENV CHD_MODE="webui"
ENV CHDMAN_MODE="createcd"
ENV MAX_CONCURRENT_JOBS=1
ENV CHD_CHDMAN_NICE=10
ENV CHD_CHDMAN_IOPRIO_CLASS=2
ENV CHD_CHDMAN_IOPRIO_LEVEL=6
ENV PYTHONUNBUFFERED=1

# Default volume mount point
VOLUME ["/data/games"]

# Expose web port
EXPOSE 8080

# Health check (only applies in webui mode)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD if [ "${CHD_MODE:-webui}" = "cli" ]; then \
            exit 0; \
        fi; \
        if [ "$(id -u)" = "0" ]; then \
            gosu converter python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"; \
        else \
            python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"; \
        fi

# Create runtime user/group (pinned to 999:999) and prepare ownership for entrypoint privilege drop
RUN groupadd -r -g 999 converter && useradd -r -u 999 -g converter -s /sbin/nologin converter \
    && chown -R converter:converter /app /static /opt/venv \
    && mkdir -p /data/games /config \
    && chown converter:converter /data/games /config

# nosemgrep: dockerfile.security.missing-user-entrypoint.missing-user-entrypoint
# The container deliberately starts as root so entrypoint.sh can honour the
# PUID/PGID env vars (`usermod`/`groupmod`/`chown` require root) before
# `exec gosu converter "$0" "$@"` drops privileges to the unprivileged
# converter user (uid 999) for the actual application.  Adding `USER` here
# would prevent the runtime UID/GID remap that lets the container write
# host-correct ownership on bind-mounted volumes.
ENTRYPOINT ["/entrypoint.sh"]
