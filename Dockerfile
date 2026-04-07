FROM debian:trixie-slim AS builder

# Install build dependencies
ENV DEBIAN_FRONTEND=noninteractive
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
ENV DEBIAN_FRONTEND=noninteractive
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
      ca-certificates && \
    # Install dolphin-emu only where available/practical
    if [ "$TARGETARCH" = "amd64" ]; then \
      apt-get install -y --no-install-recommends dolphin-emu; \
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
    apt-get install -y --no-install-recommends /tmp/mame-tools.deb && \
    rm /tmp/mame-tools.deb && \
    # --- Verify chdman version ---
    chdman 2>&1 | head -1 | grep -q "0\.285" && \
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
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install z3ds_compressor from builder stage
COPY --from=builder /tmp/z3ds/z3ds_compressor /usr/local/bin/z3ds_compressor

# Copy application
COPY app/ /app/
COPY static/ /static/
COPY .version /app/.version
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app

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
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 0

# Run as non-root user
RUN groupadd -r converter && useradd -r -g converter -s /sbin/nologin converter \
    && chown -R converter:converter /app /static /opt/venv \
    && mkdir -p /data/games /config \
    && chown converter:converter /data/games /config

USER converter

ENTRYPOINT ["/entrypoint.sh"]
