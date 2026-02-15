FROM debian:trixie-slim AS builder

# Install build dependencies
RUN apt-get update && \
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


# Install system dependencies, create wrapper script, and prepare venv
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
      mame-tools \
      python3 \
      python3-pip \
      python3-venv \
      util-linux \
      unrar-free \
      p7zip-full \
      dolphin-emu \
      wget \
      unzip \
      zstd \
      bash && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    printf '#!/bin/bash\nexec /usr/games/dolphin-tool "$@"\n' > /usr/local/bin/dolphin-tool && \
    chmod +x /usr/local/bin/dolphin-tool && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir "pip>=25.3"
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install z3ds_compressor for 3DS ROM compression
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
