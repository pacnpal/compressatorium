#!/bin/bash
set -e

# Load local environment variables
if [ -f .env.local ]; then
    set -o allexport
    # shellcheck source=/dev/null
    source .env.local
    set +o allexport
fi

# Configuration
export COMPRESSATORIUM_MOUNT_ROOT="${COMPRESSATORIUM_MOUNT_ROOT:-${CHD_MOUNT_ROOT:-$(pwd)/test_data}}"
export COMPRESSATORIUM_VOLUMES="${COMPRESSATORIUM_VOLUMES:-${CHD_VOLUMES:-}}"
export CHD_MODE="webui"
export MAX_CONCURRENT_JOBS=1
export CHD_CHDMAN_NICE=0
export CHD_CHDMAN_IOPRIO_CLASS=0
export CHD_CHDMAN_IOPRIO_LEVEL=0
STATIC_DIR="$(pwd)/static"
export STATIC_DIR
CHD_DATA_DIR="$(pwd)/.local-config"
export CHD_DATA_DIR
CHD_TEMP_DIR="$(pwd)/.local-config/temp"
export CHD_TEMP_DIR

# Ensure directories exist
mkdir -p "$CHD_DATA_DIR"
mkdir -p "$CHD_TEMP_DIR"
mkdir -p "$COMPRESSATORIUM_MOUNT_ROOT"

# Activate virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
# shellcheck source=/dev/null
source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Add app directory to PYTHONPATH
PYTHONPATH="$PYTHONPATH:$(pwd)/app"
export PYTHONPATH

# Run the application
PORT="${PORT:-8080}"
echo "Starting Compressatorium on port $PORT..."
echo "Volume root: $COMPRESSATORIUM_MOUNT_ROOT"
if [ -n "$COMPRESSATORIUM_VOLUMES" ]; then
    echo "Using explicit COMPRESSATORIUM_VOLUMES: $COMPRESSATORIUM_VOLUMES"
else
    echo "Using auto-discovery under: $COMPRESSATORIUM_MOUNT_ROOT"
fi
echo "Config: $CHD_DATA_DIR"
echo "Access at: http://localhost:$PORT"

# Run module directly since app is in PYTHONPATH
python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --reload
