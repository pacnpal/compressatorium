#!/bin/bash
set -e

# Load local environment variables
if [ -f .env.local ]; then
    set -o allexport
    source .env.local
    set +o allexport
fi

# Configuration
export CHD_VOLUMES="${CHD_VOLUMES:-$(pwd)/test_data}"
export CHD_MODE="webui"
export MAX_CONCURRENT_JOBS=1
export CHD_CHDMAN_NICE=0
export CHD_CHDMAN_IOPRIO_CLASS=0
export CHD_CHDMAN_IOPRIO_LEVEL=0
export STATIC_DIR="$(pwd)/static"
export CHD_DATA_DIR="$(pwd)/.local-config"
export CHD_TEMP_DIR="$(pwd)/.local-config/temp"

# Ensure directories exist
mkdir -p "$CHD_DATA_DIR"
mkdir -p "$CHD_TEMP_DIR"
mkdir -p "$CHD_VOLUMES"

# Activate virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Add app directory to PYTHONPATH
export PYTHONPATH="$PYTHONPATH:$(pwd)/app"

# Run the application
PORT="${PORT:-8080}"
echo "Starting Compressatorium on port $PORT..."
echo "Volumes: $CHD_VOLUMES"
echo "Config: $CHD_DATA_DIR"
echo "Access at: http://localhost:$PORT"

# Run module directly since app is in PYTHONPATH
python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --reload
