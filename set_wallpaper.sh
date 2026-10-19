#!/bin/bash
#
# DIY XPlanet - set the generated Earth view as the desktop wallpaper.
#
# Usage: ./set_wallpaper.sh [image]
#        Defaults to whatever earth_daynight_latest.png points at.
#
# Two things make hourly wallpaper updates stick where naive approaches fail:
#
# 1. It resolves the symlink first. macOS registers the desktop picture by file
#    path, so handing it "earth_daynight_latest.png" again is a no-op even
#    though the bytes behind it changed. Each run writes a uniquely named file;
#    passing that real path gives macOS a path it has not seen before.
#
# 2. It verifies and retries. Setting the picture is asynchronous and does not
#    reliably reach every display on the first attempt, so this reads the value
#    back and tries again until all displays agree.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${1:-$PROJECT_DIR/earth_daynight_latest.png}"
ATTEMPTS=5

if [ ! -e "$IMAGE" ]; then
    echo "Error: image not found: $IMAGE" >&2
    exit 1
fi

# Resolve symlinks to the unique per-run filename.
IMAGE=$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$IMAGE")
echo "Setting wallpaper to: $IMAGE"

set_all_displays() {
    if command -v desktoppr >/dev/null 2>&1; then
        # Purpose-built tool, covers every display in one call.
        desktoppr "$1" >/dev/null 2>&1
    else
        # Index the desktops explicitly; "repeat with d in desktops" has been
        # observed to update only the first display.
        /usr/bin/osascript -e 'on run argv
    tell application "System Events"
        repeat with i from 1 to count of desktops
            set picture of desktop i to (item 1 of argv)
        end repeat
    end tell
end run' "$1" >/dev/null 2>&1
    fi
}

current_wallpapers() {
    /usr/bin/osascript -e 'tell application "System Events" to get picture of every desktop' 2>/dev/null
}

for attempt in $(seq 1 $ATTEMPTS); do
    set_all_displays "$IMAGE"
    sleep 1

    current=$(current_wallpapers)
    # Every display agrees once no comma-separated entry differs from IMAGE.
    stale=$(printf '%s' "$current" | tr ',' '\n' | sed 's/^ *//' | grep -vcx -F "$IMAGE")

    if [ "$stale" -eq 0 ]; then
        echo "✓ Wallpaper set on all displays (attempt $attempt)"
        if ! command -v desktoppr >/dev/null 2>&1; then
            echo "  Tip: 'brew install --cask desktoppr' makes this a single reliable call."
        fi
        exit 0
    fi

    echo "  Attempt $attempt: $stale display(s) not updated yet, retrying..."
done

echo "Error: wallpaper did not apply to all displays after $ATTEMPTS attempts" >&2
echo "  macOS currently reports: $(current_wallpapers)" >&2
exit 1
