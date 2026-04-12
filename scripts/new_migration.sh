#!/usr/bin/env bash
# Generate a new Alembic migration by autogeneration.
#
# Usage:
#   scripts/new_migration.sh "describe the change in a short phrase"
#
# The script invokes alembic with a throwaway scratch DB so we don't
# touch the real compressatorium.db.  The resulting migration file is
# written to migrations/versions/ — review it carefully (autogenerate
# misses CHECK constraints, server_default=, and some index details)
# before committing.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 \"message describing the schema change\"" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH_DB="$(mktemp -u -t compressatorium_scratch.XXXXXX).db"

cleanup() {
  rm -f "$SCRATCH_DB" "${SCRATCH_DB}-journal" "${SCRATCH_DB}-wal" "${SCRATCH_DB}-shm" 2>/dev/null || true
}
trap cleanup EXIT

# Apply the current migration chain to the scratch DB so autogenerate
# has a correct "before" state to diff against Base.metadata.
PYTHONPATH="$REPO_ROOT/app" \
  COMPRESSATORIUM_ALEMBIC_URL="sqlite:///$SCRATCH_DB" \
  python3 -m alembic -c "$REPO_ROOT/migrations/alembic.ini" upgrade head

PYTHONPATH="$REPO_ROOT/app" \
  COMPRESSATORIUM_ALEMBIC_URL="sqlite:///$SCRATCH_DB" \
  python3 -m alembic -c "$REPO_ROOT/migrations/alembic.ini" \
    revision --autogenerate -m "$*"

echo
echo "Generated — review the new file under migrations/versions/ before committing."
