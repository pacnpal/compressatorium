#!/usr/bin/env bash
# Sync version from .version file to all version references
# Usage: ./scripts/sync-version.sh [new-version]
#
# If new-version is provided, updates .version first, then syncs to other files.
# If no argument, reads from .version and syncs to other files.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

VERSION_FILE="$PROJECT_ROOT/.version"

# Get or set version
if [ -n "$1" ]; then
    echo "$1" > "$VERSION_FILE"
    VERSION="$1"
    echo "Set version to: $VERSION"
else
    if [ ! -f "$VERSION_FILE" ]; then
        echo "Error: .version file not found"
        exit 1
    fi
    VERSION=$(cat "$VERSION_FILE" | tr -d '\n\r ')
    echo "Read version: $VERSION"
fi

# Validate version format (semver)
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$ ]]; then
    echo "Error: Invalid version format. Expected semver (e.g., 1.2.3 or 1.2.3-beta.1)"
    exit 1
fi

# Update package.json if it exists
PACKAGE_JSON="$PROJECT_ROOT/package.json"
if [ -f "$PACKAGE_JSON" ]; then
    # Use sed to update version in package.json
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' "s/\"version\": \"[^\"]*\"/\"version\": \"$VERSION\"/" "$PACKAGE_JSON"
    else
        # Linux
        sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"$VERSION\"/" "$PACKAGE_JSON"
    fi
    echo "Updated package.json"
fi

echo ""
echo "Version $VERSION synced to all files."
echo ""
echo "Files using .version as source of truth:"
echo "  - FastAPI app (reads at runtime)"
echo "  - Frontend footer (fetches from /api/version)"
echo "  - Docker image tags (reads in CI workflow)"
echo "  - Health endpoint (returns version)"
echo ""
echo "Files that need manual sync (updated by this script):"
echo "  - package.json"
