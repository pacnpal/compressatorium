#!/bin/bash
set -e

# Check if /config is mounted
if ! mountpoint -q /config 2>/dev/null; then
    echo ""
    echo "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
    echo "┃  🚨 WHOOPS! You forgot to mount /config!                                ┃"
    echo "┃                                                                         ┃"
    echo "┃  Your verification data is going to disappear when this container       ┃"
    echo "┃  restarts. Like tears in rain. Gone. Poof. Vanished.                    ┃"
    echo "┃                                                                         ┃"
    echo "┃  Fix it:  -v /path/to/config:/config                                    ┃"
    echo "┃                                                                         ┃"
    echo "┃  Docs: https://github.com/pacnpal/docker-chd-converter-webui            ┃"
    echo "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    echo ""
fi

# CHD_MODE: "webui" (default) or "cli"
MODE="${CHD_MODE:-webui}"

if [[ "$MODE" == "cli" ]]; then
    # Original CLI behavior - convert all files in volumes
    CHDMAN_MODE="${CHDMAN_MODE:-createcd}"

    case "$CHDMAN_MODE" in
        cd)  CHDMAN_MODE="createcd"  ;;
        dvd) CHDMAN_MODE="createdvd" ;;
        createcd|createdvd) ;;
        *) echo "Unsupported CHDMAN_MODE: '$CHDMAN_MODE'. Use 'createcd' or 'createdvd'." >&2; exit 1 ;;
    esac

    echo "Running in CLI mode with CHDMAN_MODE=$CHDMAN_MODE"
    echo "Volumes: $CHD_VOLUMES"

    for volume in ${CHD_VOLUMES//,/ }; do
        if [[ -d "$volume" ]]; then
            echo "Processing volume: $volume"
            cd "$volume"
            shopt -s nullglob
            for i in *.gdi *.iso *.cue; do
                [[ -e "$i" ]] || continue
                [[ -e "${i%.*}.chd" ]] && { echo "Skipping '$i' (CHD exists)."; continue; }
                echo "Converting '$i' using chdman ${CHDMAN_MODE} ..."
                if [[ "$CHDMAN_MODE" == "createdvd" ]]; then
                    chdman createdvd -hs 2048 -f -i "$i" -o "${i%.*}.chd"
                else
                    chdman createcd -f -i "$i" -o "${i%.*}.chd"
                fi
            done
        else
            echo "Warning: Volume '$volume' does not exist, skipping."
        fi
    done
    echo "CLI conversion complete."
else
    # Web UI mode (default)
    echo "Starting CHD Converter Web UI..."
    echo "Volumes: $CHD_VOLUMES"
    echo "Access the web interface at http://localhost:8080"
    exec uvicorn main:app --host 0.0.0.0 --port 8080
fi
