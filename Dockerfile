FROM debian:trixie-slim

# Install system dependencies
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
      mame-tools \
      python3 \
      python3-pip \
      python3-venv \
      unrar-free \
      p7zip-full \
      bash && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create Python virtual environment and install dependencies
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application
COPY app/ /app/
COPY static/ /static/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app

# Configuration
ENV CHD_VOLUMES="/data/games"
ENV CHD_MODE="webui"
ENV CHDMAN_MODE="createcd"
ENV MAX_CONCURRENT_JOBS=2
ENV PYTHONUNBUFFERED=1

# Default volume mount point
VOLUME ["/data/games"]

# Expose web port
EXPOSE 8080

# Health check (only applies in webui mode)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 0

ENTRYPOINT ["/entrypoint.sh"]
