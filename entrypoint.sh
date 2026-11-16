#!/bin/bash
set -e

# Give owner:group to every entry under the given paths that doesn't already
# have it (#350), so a restart rewrites nothing that is already right.
# Never follows a symlink: find walks with its default -P and chown -h
# changes the link itself. An entry that vanishes during the walk is fine:
# SQLite's -wal/-shm files come and go while another container uses the same
# /config. Any other error is printed and returns 1, which stops the
# container on purpose: the app can't run on files it can't own.
fix_ownership() {
    owner="$1"
    group="$2"
    shift 2
    errors="$(LC_ALL=C /usr/bin/find "$@" \( ! -user "$owner" -o ! -group "$group" \) \
        -exec /usr/bin/chown -h "$owner:$group" {} + 2>&1 >/dev/null)" || true
    errors="$(/usr/bin/grep -v -e ': No such file or directory$' -e '^$' <<<"$errors")" || true
    if [ -n "$errors" ]; then
        echo "Could not set the owner of these files to $owner:$group:" >&2
        echo "$errors" >&2
        return 1
    fi
    return 0
}

# Remap converter UID/GID to PUID/PGID then drop privileges
if [ "$(/usr/bin/id -u)" = "0" ]; then
    PUID=${PUID:-999}
    PGID=${PGID:-999}
    ownership_changed=0

    if ! [[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ ]] || [ "$PUID" -eq 0 ] || [ "$PGID" -eq 0 ]; then
        echo "Invalid PUID/PGID. Both must be numeric and greater than 0." >&2
        exit 1
    fi

    if [ "$(/usr/bin/id -g converter)" != "$PGID" ]; then
        if ! groupmod_error="$(/usr/sbin/groupmod -g "$PGID" converter 2>&1)"; then
            if /usr/bin/getent group "$PGID" >/dev/null; then
                echo "GID $PGID already exists; assigning converter to the existing group." >&2
                /usr/sbin/usermod -g "$PGID" converter
            else
                echo "Failed to remap converter to PGID $PGID: $groupmod_error" >&2
                exit 1
            fi
        fi
        ownership_changed=1
    fi
    if [ "$(/usr/bin/id -u converter)" != "$PUID" ]; then
        if /usr/bin/getent passwd "$PUID" >/dev/null; then
            echo "Cannot remap converter to PUID $PUID: UID already exists." >&2
            exit 1
        fi
        /usr/sbin/usermod -u "$PUID" converter
        ownership_changed=1
    fi

    if [ "$ownership_changed" = "1" ]; then
        # /config is the app's own state (its database, temp files, locks), so
        # it is always brought to PUID:PGID, mounted or not: the app must be
        # able to write it.
        paths_to_fix=(/app /static /opt/venv /config)
        # /data/games, once mounted, is the user's library: its ownership is
        # never rewritten. Only the image's own empty folder is fixed. (The
        # old check looked for "bind" in findmnt's OPTIONS, which never lists
        # it, so it never matched and every mount was rewritten.)
        if [ -e /data/games ] && ! /usr/bin/mountpoint -q /data/games 2>/dev/null; then
            paths_to_fix+=(/data/games)
        fi
        fix_ownership "$PUID" "$(/usr/bin/id -g converter)" "${paths_to_fix[@]}"
    fi
    unset PUID PGID
    exec /usr/sbin/gosu converter "$0" "$@"
elif [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then
    echo "Warning: PUID/PGID remap requires container startup as root; current UID is $(/usr/bin/id -u). Ignoring PUID/PGID." >&2
fi

# `/entrypoint.sh compressatorium ...`: what the `compressatorium` command runs
# when it starts as root (`docker exec`, `--entrypoint compressatorium`). The
# block above has remapped PUID/PGID and dropped to converter; run the command
# now, before anything below prints to stdout (its --json output).
if [ "${1:-}" = "compressatorium" ]; then
    shift
    exec /usr/local/bin/compressatorium "$@"
fi

# Check if /config is mounted
if ! mountpoint -q /config 2>/dev/null; then
    echo "" >&2
    echo "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓" >&2
    echo "┃  🚨 WHOOPS! You forgot to mount /config!                                ┃" >&2
    echo "┃                                                                         ┃" >&2
    echo "┃  Your verification data is going to disappear when this container       ┃" >&2
    echo "┃  restarts. Like tears in rain. Gone. Poof. Vanished.                    ┃" >&2
    echo "┃                                                                         ┃" >&2
    echo "┃  Fix it:  -v /path/to/config:/config                                    ┃" >&2
    echo "┃                                                                         ┃" >&2
    echo "┃  Docs: https://github.com/pacnpal/Compressatorium                      ┃" >&2
    echo "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛" >&2
    echo "" >&2
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
    # CHD_MODE=cli: convert every Volume to CHD, then exit
    CHDMAN_MODE="${CHDMAN_MODE:-createcd}"

    case "$CHDMAN_MODE" in
        cd)  CHDMAN_MODE="createcd"  ;;
        dvd) CHDMAN_MODE="createdvd" ;;
        createcd|createdvd) ;;
        *) echo "Unsupported CHDMAN_MODE: '$CHDMAN_MODE'. Use 'createcd' or 'createdvd'." >&2; exit 1 ;;
    esac

    echo "Running in CLI mode with CHDMAN_MODE=$CHDMAN_MODE" >&2
    echo "Volume root: $MOUNT_ROOT" >&2
    if [[ -n "$EXPLICIT_VOLUMES" ]]; then
        echo "Using explicit COMPRESSATORIUM_VOLUMES: $VOLUMES_EFFECTIVE" >&2
    else
        echo "Discovered volumes: $VOLUMES_EFFECTIVE" >&2
    fi

    # Alias for `compressatorium convert <each Volume> --to $CHDMAN_MODE`: the
    # same planner, job queue, locks and Ledger as the web UI. Existing outputs
    # are skipped, as before.
    IFS=',' read -r -a volume_list <<< "$VOLUMES_EFFECTIVE"
    existing=()
    for volume in "${volume_list[@]}"; do
        # Trim incidental whitespace around comma-separated entries.
        volume="${volume#"${volume%%[![:space:]]*}"}"
        volume="${volume%"${volume##*[![:space:]]}"}"
        [[ -z "$volume" ]] && continue
        if [[ -d "$volume" ]]; then
            echo "Processing volume: $volume" >&2
            existing+=("$volume")
        else
            echo "Warning: Volume '$volume' does not exist, skipping." >&2
        fi
    done
    if [[ ${#existing[@]} -eq 0 ]]; then
        echo "No volumes to convert." >&2
        exit 0
    fi
    # Today's scope and defaults: the top level of each Volume, no archives,
    # no Platform check, chdman's own compression.
    exec compressatorium convert "${existing[@]}" --to "$CHDMAN_MODE" --no-recurse --no-archives --any-platform --compression default
else
    # Web UI mode (default)
    echo "Starting CHD Converter Web UI..." >&2
    echo "Volume root: $MOUNT_ROOT" >&2
    if [[ -n "$EXPLICIT_VOLUMES" ]]; then
        echo "Using explicit COMPRESSATORIUM_VOLUMES: $VOLUMES_EFFECTIVE" >&2
    else
        echo "Discovered volumes: $VOLUMES_EFFECTIVE" >&2
    fi
    echo "Access the web interface at http://localhost:8080" >&2
    exec uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1
fi
