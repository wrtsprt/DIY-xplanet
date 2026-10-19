#!/bin/bash

# DIY XPlanet  
# This script generates a new Earth view.

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CENTER_LON=-10
# Viewpoint latitude. Lower values look at Earth from further south and so show
# less over the top of the globe: the far horizon along the central meridian
# sits at (90 - CENTER_LAT) degrees north. At 35 that was 55N, which put the
# smeared polar cloud data in full view; 20 pushes it to 70N, out of sight.
CENTER_LAT=20
OUTPUT_SIZE=4096
# Renders are ~15 MB each. Keep this many in output/final; older ones are pruned.
KEEP_OUTPUTS=48
# Days of downloaded cloud data to keep in ~/.cache/diy-xplanet.
CACHE_DAYS=2
# Never pad narrower than 16:9, so plugging in a typical widescreen display
# later does not start cropping the disk before this is re-run.
MIN_ASPECT=1.7778

# Navigate to project directory
cd "$PROJECT_DIR" || exit 1

# Activate virtual environment if it exists
if [ -f "$PROJECT_DIR/venv/bin/activate" ]; then
    source "$PROJECT_DIR/venv/bin/activate"
fi

# Work out how wide the canvas has to be. macOS scales the wallpaper with
# "Fill" unless something sets the placement explicitly, which crops a square
# image on a widescreen display - so the render is padded to at least the
# aspect of the widest attached display and only black bars get cropped.
CANVAS_ASPECT=$(system_profiler SPDisplaysDataType -json 2>/dev/null | python3 -c "
import json, sys
floor = $MIN_ASPECT
best = floor
try:
    data = json.load(sys.stdin)
except Exception:
    print(f'{floor:.4f}'); raise SystemExit
for gpu in data.get('SPDisplaysDataType', []):
    for disp in gpu.get('spdisplays_ndrvs') or []:
        px = disp.get('_spdisplays_pixels', '')
        try:
            w, h = (int(n) for n in px.lower().split(' x '))
        except ValueError:
            continue
        if h:
            best = max(best, w / h)
print(f'{best:.4f}')
" 2>/dev/null)
# Fall back to the floor if detection produced nothing usable.
case "$CANVAS_ASPECT" in
    ''|*[!0-9.]*) CANVAS_ASPECT=$MIN_ASPECT ;;
esac
echo "Canvas aspect: $CANVAS_ASPECT"

# Generate new Earth view
echo "Generating new Earth view..."
python3 create_daynight_earth.py \
    --clouds \
    --center $CENTER_LON $CENTER_LAT \
    --size $OUTPUT_SIZE \
    --canvas-aspect $CANVAS_ASPECT \
    --keep-outputs $KEEP_OUTPUTS \
    --cache-days $CACHE_DAYS

# Check if generation was successful
if [ $? -ne 0 ]; then
    echo "Error: Failed to generate Earth view"
    exit 1
fi

# Apply it. This resolves the symlink to the unique filename this run produced,
# which is what makes the update actually take effect, and verifies every
# display picked it up. With this in place the Keyboard Maestro macro needs only
# the "Execute Shell Script" action - its two "Set Desktop Image" actions
# (pointed at the unchanging symlink path) can be removed.
"$PROJECT_DIR/set_wallpaper.sh" || exit 1

echo "✓ Desktop background generated and applied!"
