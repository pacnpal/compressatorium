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
    echo "┃  Docs: https://github.com/pacnpal/Compressatorium                      ┃"
    echo "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    echo ""
fi

# CHD_MODE: "webui" (default) or "cli"
MODE="${CHD_MODE:-webui}"
MOUNT_ROOT="${COMPRESSATORIUM_MOUNT_ROOT:-${CHD_MOUNT_ROOT:-/data}}"
EXPLICIT_VOLUMES="${COMPRESSATORIUM_VOLUMES:-${CHD_VOLUMES:-}}"
VOLUMES_EFFECTIVE=""

discover_volumes() {
    local root="$1"
    local explicit="$2"
    local -a dirs=()
    local -a mount_dirs=()
    local -a selected=()

    if [[ -n "$explicit" ]]; then
        echo "$explicit"
        return
    fi

    if [[ -d "$root" ]]; then
        while IFS= read -r dir; do
            dirs+=("$dir")
            if mountpoint -q "$dir" 2>/dev/null; then
                mount_dirs+=("$dir")
            fi
        done < <(find "$root" -mindepth 1 -maxdepth 1 -type d | sort)

        if [[ ${#mount_dirs[@]} -gt 0 ]]; then
            selected=("${mount_dirs[@]}")
        elif [[ ${#dirs[@]} -gt 0 ]]; then
            selected=("${dirs[@]}")
        else
            selected=("$root")
        fi
    fi

    local out=""
    local v
    for v in "${selected[@]}"; do
        if [[ -z "$v" ]]; then
            continue
        fi
        if [[ -n "$out" ]]; then
            out="$out,$v"
        else
            out="$v"
        fi
    done
    echo "$out"
}

VOLUMES_EFFECTIVE="$(discover_volumes "$MOUNT_ROOT" "$EXPLICIT_VOLUMES")"

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
    echo "Volume root: $MOUNT_ROOT"
    if [[ -n "$EXPLICIT_VOLUMES" ]]; then
        echo "Using explicit COMPRESSATORIUM_VOLUMES: $VOLUMES_EFFECTIVE"
    else
        echo "Discovered volumes: $VOLUMES_EFFECTIVE"
    fi

    IFS=',' read -r -a volume_list <<< "$VOLUMES_EFFECTIVE"
    for volume in "${volume_list[@]}"; do
        # Trim incidental whitespace around comma-separated entries.
        volume="${volume#"${volume%%[![:space:]]*}"}"
        volume="${volume%"${volume##*[![:space:]]}"}"
        [[ -z "$volume" ]] && continue
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
    echo "Volume root: $MOUNT_ROOT"
    if [[ -n "$EXPLICIT_VOLUMES" ]]; then
        echo "Using explicit COMPRESSATORIUM_VOLUMES: $VOLUMES_EFFECTIVE"
    else
        echo "Discovered volumes: $VOLUMES_EFFECTIVE"
    fi
    echo "Access the web interface at http://localhost:8080"
    exec uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1
fi
