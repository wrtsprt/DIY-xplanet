#!/usr/bin/env python3
"""
Generate a full-disk Earth view by blending day and night imagery along the terminator.

This script:
1. Downloads NASA Blue Marble (day) and Black Marble (night) textures
2. Calculates the current solar position and terminator line
3. Blends day/night textures with smooth transition along the terminator
4. Projects to orthographic view centered on a specified location

Usage:
    python create_daynight_earth.py                    # Europe-centered view
    python create_daynight_earth.py --center 10 55     # Custom center (lon, lat)
    python create_daynight_earth.py --size 2048        # Higher resolution
    python create_daynight_earth.py --time "2026-01-15 12:00:00"  # Specific time
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlretrieve, Request, urlopen
from urllib.error import URLError, HTTPError
import math
from typing import Tuple

try:
    from PIL import Image, ImageDraw
    import numpy as np
except ImportError:
    print("Error: This script requires Pillow and numpy.", file=sys.stderr)
    print("Install with: pip install Pillow numpy", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# Texture URLs
# =============================================================================

# NASA Blue Marble Next Generation (day texture) - Monthly variations
# Using 5400x2700 composites for good quality without huge file size
# Each month shows seasonal vegetation, snow cover, and environmental changes
# Format: (image_record_number, month_code)
BLUE_MARBLE_MONTHLY_URLS = {
    1: (73580, "200401"),   # January
    2: (73605, "200402"),   # February
    3: (73630, "200403"),   # March
    4: (73655, "200404"),   # April
    5: (73701, "200405"),   # May
    6: (73726, "200406"),   # June
    7: (73751, "200407"),   # July
    8: (73776, "200408"),   # August
    9: (73801, "200409"),   # September
    10: (73826, "200410"),  # October
    11: (73884, "200411"),  # November
    12: (73909, "200412"),  # December
}

def get_blue_marble_url(month: int) -> str:
    """Get the Blue Marble URL for a specific month (1-12)."""
    record_num, month_code = BLUE_MARBLE_MONTHLY_URLS[month]
    return f"https://eoimages.gsfc.nasa.gov/images/imagerecords/73000/{record_num}/world.topo.bathy.{month_code}.3x5400x2700.jpg"

# NASA Black Marble (night lights)
# Using a composite night lights texture (annual, not seasonal)
BLACK_MARBLE_URL = "https://eoimages.gsfc.nasa.gov/images/imagerecords/144000/144898/BlackMarble_2016_01deg.jpg"

# NASA GIBS - Global Imagery Browse Services
# Polar-orbiting satellites with excellent global coverage
GIBS_WMS_BASE = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"

# Cloud Fraction layers (cloud-only data, no Earth surface)
# Red channel = cloud fraction (0=clear, 255=full clouds)
GIBS_LAYER_CLOUD_FRACTION_TERRA_DAY = "MODIS_Terra_Cloud_Fraction_Day"
GIBS_LAYER_CLOUD_FRACTION_TERRA_NIGHT = "MODIS_Terra_Cloud_Fraction_Night"
GIBS_LAYER_CLOUD_FRACTION_AQUA_DAY = "MODIS_Aqua_Cloud_Fraction_Day"
GIBS_LAYER_CLOUD_FRACTION_AQUA_NIGHT = "MODIS_Aqua_Cloud_Fraction_Night"

# Legacy True Color layers (kept for reference, not used)
GIBS_LAYER_VIIRS = "VIIRS_SNPP_CorrectedReflectance_TrueColor"  # Has stitching artifacts
GIBS_LAYER_TERRA = "MODIS_Terra_CorrectedReflectance_TrueColor"
GIBS_LAYER_AQUA = "MODIS_Aqua_CorrectedReflectance_TrueColor"

# NOAA GMGSI (Global Mosaic of Geostationary Satellite Imagery) - Backup option
# WMS service providing hourly updated global cloud imagery
GMGSI_WMS_BASE = "https://nowcoast.noaa.gov/geoserver/satellite/wms"
GMGSI_LAYER = "global_visible_imagery_mosaic"


# =============================================================================
# Solar Position Calculation
# =============================================================================

def calculate_sun_position(dt: datetime) -> Tuple[float, float]:
    """
    Calculate the subsolar point (latitude, longitude) for a given datetime.

    This is where the sun is directly overhead at the specified time.
    Uses simplified astronomical calculations.

    Returns:
        (latitude, longitude) in degrees
    """
    # Convert to Julian Date
    a = (14 - dt.month) // 12
    y = dt.year + 4800 - a
    m = dt.month + 12 * a - 3

    jdn = dt.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd = jdn + (dt.hour - 12) / 24.0 + dt.minute / 1440.0 + dt.second / 86400.0

    # Days since J2000.0
    n = jd - 2451545.0

    # Mean longitude of the Sun
    L = (280.460 + 0.9856474 * n) % 360

    # Mean anomaly of the Sun
    g = (357.528 + 0.9856003 * n) % 360
    g_rad = math.radians(g)

    # Ecliptic longitude
    lambda_sun = L + 1.915 * math.sin(g_rad) + 0.020 * math.sin(2 * g_rad)
    lambda_rad = math.radians(lambda_sun)

    # Obliquity of the ecliptic
    epsilon = 23.439 - 0.0000004 * n
    epsilon_rad = math.radians(epsilon)

    # Declination (solar latitude)
    dec = math.degrees(math.asin(math.sin(epsilon_rad) * math.sin(lambda_rad)))

    # Right ascension
    ra = math.degrees(math.atan2(math.cos(epsilon_rad) * math.sin(lambda_rad), math.cos(lambda_rad)))

    # Greenwich Mean Sidereal Time
    gmst = (18.697374558 + 24.06570982441908 * n) % 24

    # Solar longitude (where sun is overhead)
    solar_lon = (ra - gmst * 15) % 360
    if solar_lon > 180:
        solar_lon -= 360

    return dec, solar_lon


def calculate_terminator_distance(lat: float, lon: float, sun_lat: float, sun_lon: float) -> float:
    """
    Calculate the angular distance from a point to the terminator.

    Returns:
        Angular distance in degrees (positive = day side, negative = night side)
    """
    # Convert to radians
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.radians(sun_lat)
    lon2 = math.radians(sun_lon)

    # Great circle distance to subsolar point
    dlon = lon2 - lon1

    # Haversine formula
    a = math.sin((lat2 - lat1) / 2) ** 2 + \
        math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))

    # Convert to degrees
    angular_dist = math.degrees(c)

    # Return distance from terminator (90 degrees from subsolar point)
    # Positive = day side, negative = night side
    return 90 - angular_dist


# =============================================================================
# Texture Management
# =============================================================================

def download_texture(url: str, cache_path: Path, description: str) -> Path:
    """Download a texture if not already cached."""
    if cache_path.exists():
        print(f"✓ Using cached {description}: {cache_path}")
        return cache_path

    print(f"Downloading {description}...")
    print(f"  URL: {url}")
    print(f"  Saving to: {cache_path}")

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        req = Request(url, headers={"User-Agent": "diy-xplanet/1.0"})
        with urlopen(req, timeout=120) as response:
            with open(cache_path, 'wb') as f:
                f.write(response.read())

        size_mb = cache_path.stat().st_size / 1024 / 1024
        print(f"✓ Downloaded {description} ({size_mb:.1f} MB)")
        return cache_path

    except (HTTPError, URLError) as e:
        print(f"✗ Failed to download: {e}", file=sys.stderr)
        sys.exit(1)


def load_and_prepare_textures(cache_dir: Path, month: int) -> Tuple[Image.Image, Image.Image]:
    """Download and load both day and night textures.

    Args:
        cache_dir: Directory for caching textures
        month: Month number (1-12) for selecting seasonal Blue Marble texture
    """
    # Get the appropriate Blue Marble URL for this month
    blue_marble_url = get_blue_marble_url(month)

    # Download textures (cache by month so we get seasonal variations)
    day_texture_path = download_texture(
        blue_marble_url,
        cache_dir / f"blue_marble_day_month{month:02d}.jpg",
        f"Blue Marble (day, month {month})"
    )

    night_texture_path = download_texture(
        BLACK_MARBLE_URL,
        cache_dir / "black_marble_night.jpg",
        "Black Marble (night)"
    )

    # Load images
    print("\nLoading textures...")
    day_img = Image.open(day_texture_path)
    night_img = Image.open(night_texture_path)

    # Ensure both are same size
    if day_img.size != night_img.size:
        print(f"  Resizing night texture to match day texture {day_img.size}")
        night_img = night_img.resize(day_img.size, Image.Resampling.LANCZOS)

    print(f"✓ Loaded textures: {day_img.size[0]}x{day_img.size[1]}")

    return day_img, night_img


def cleanup_old_cache(cache_dir: Path, days: int = 3):
    """Remove cached cloud data older than specified days."""
    try:
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(days=days)

        for cache_file in cache_dir.glob("clouds_*"):
            if cache_file.is_file():
                file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
                if file_time < cutoff:
                    cache_file.unlink()
                    print(f"  Cleaned up old cache: {cache_file.name}")

        # Zero-byte entries are failed downloads. The Blue Marble textures are
        # keyed by month, so a truncated one would be trusted for a whole month.
        for cache_file in cache_dir.glob("*"):
            if cache_file.is_file() and cache_file.stat().st_size == 0:
                cache_file.unlink()
                print(f"  Removed empty cache file: {cache_file.name}")
    except Exception as e:
        print(f"  Warning: Failed to clean cache: {e}")


def fit_to_canvas(img: Image.Image, aspect: float) -> Image.Image:
    """Centre the (square) Earth view on a black canvas of the given aspect.

    macOS scales the desktop picture with "Fill" unless something explicitly
    sets the placement, and Fill scales to *cover* the screen - so a square
    render on a wider screen gets its top and bottom cropped off, cutting the
    poles. Keyboard Maestro's "Set Desktop Image" action used to set the
    placement to FitToScreen for us; plain AppleScript cannot.

    Padding the render out to at least the screen's aspect makes the two
    placement modes agree: Fill now has only black side bars to crop, so the
    full disk is visible either way.
    """
    if aspect <= 0:
        return img

    img = img.convert("RGB")
    width, height = img.size
    target_width = int(round(height * aspect))
    if target_width <= width:
        return img

    canvas = Image.new("RGB", (target_width, height), (0, 0, 0))
    canvas.paste(img, ((target_width - width) // 2, 0))
    print(f"\u2713 Padded to {target_width}x{height} (aspect {aspect:.3f}) "
          f"so 'Fill' scaling cannot crop the disk")
    return canvas


def prune_outputs(output_dir: Path, keep: int, protect: Path = None):
    """Keep only the newest `keep` generated images in output_dir.

    Every run writes a ~15 MB PNG, so this directory - not the download cache -
    is what actually grows without bound. `protect` is never removed; it is the
    image the wallpaper symlink points at.
    """
    if keep <= 0:
        return
    try:
        protected = protect.resolve() if protect else None
        images = sorted(
            (p for p in output_dir.glob("earth_*.png") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        freed = 0
        for stale in images[keep:]:
            if protected and stale.resolve() == protected:
                continue
            freed += stale.stat().st_size
            stale.unlink()
        if freed:
            print(f"\u2713 Pruned old outputs: freed {freed / 1024 / 1024:.0f} MB "
                  f"(keeping newest {keep})")
    except Exception as e:
        print(f"  Warning: Failed to prune outputs: {e}")


def download_cloud_layer(size: int = 5400, use_gibs: bool = True, combine_modis: bool = False, hybrid_polar: bool = False, cache_dir: Path = None, cache_days: int = 2) -> Image.Image:
    """
    Download current cloud imagery from Matteason live cloud maps.

    Cloud data source: EUMETSAT satellites, processed by Matt Eason
    Updates: Every 3 hours
    License: "Contains modified EUMETSAT data"

    Cloud data is cached per 3-hour window to avoid redundant downloads.

    Args:
        size: Target width of output image (will resize from 8192x4096 source)
        use_gibs: DEPRECATED - ignored, kept for compatibility
        combine_modis: DEPRECATED - ignored, kept for compatibility
        hybrid_polar: DEPRECATED - ignored, kept for compatibility
        cache_dir: Directory for caching cloud data (default: ~/.cache/diy-xplanet)
        cache_days: Days of cloud data to retain in the cache

    Returns:
        Equirectangular RGBA image with alpha channel = cloud coverage.
    """
    # Set up cache directory
    if cache_dir is None:
        cache_dir = Path.home() / ".cache" / "diy-xplanet"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Matteason clouds update every 3 hours
    # Cache key is based on 3-hour window
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cache_hour = (now.hour // 3) * 3  # Round down to nearest 3-hour mark
    cache_key = f"{now.year:04d}{now.month:02d}{now.day:02d}_{cache_hour:02d}00"

    cache_file = cache_dir / f"clouds_matteason_{cache_key}_{size}.png"

    # Prune before serving: the download path is only reached once per 3-hour
    # cache window, so cleaning up only there let the cache drift upward.
    cleanup_old_cache(cache_dir, days=cache_days)

    # Check if cached version exists
    if cache_file.exists():
        try:
            cached_img = Image.open(cache_file)
            print(f"\n✓ Using cached Matteason cloud data ({cache_key})")
            print(f"  File: {cache_file.name}")
            print(f"  Size: {cached_img.size[0]}x{cached_img.size[1]}")
            return cached_img.convert("RGBA")
        except Exception as e:
            print(f"Warning: Failed to load cached clouds, re-downloading: {e}")
            cache_file.unlink()  # Delete corrupted cache

    # Download from Matteason live cloud maps
    print("\nDownloading Matteason live cloud map...")
    print("  Source: EUMETSAT (via clouds.matteason.co.uk)")
    print("  License: Contains modified EUMETSAT data")
    print("  Updates: Every 3 hours")

    # Download highest resolution available
    url = "https://clouds.matteason.co.uk/images/8192x4096/clouds-alpha.png"

    try:
        req = Request(url, headers={"User-Agent": "diy-xplanet/1.0"})
        with urlopen(req, timeout=120) as response:
            from io import BytesIO
            data = response.read()
            img = Image.open(BytesIO(data))

        size_mb = len(data) / 1024 / 1024
        print(f"✓ Downloaded {size_mb:.1f} MB")
        print(f"  Original size: {img.size[0]}x{img.size[1]}")

        # Resize to target resolution if needed
        target_size = (size, size // 2)
        if img.size != target_size:
            print(f"  Resizing to: {target_size[0]}x{target_size[1]}")
            img = img.resize(target_size, Image.Resampling.LANCZOS)

        # Cache the downloaded/resized image
        try:
            img.save(cache_file, "PNG")
            print(f"✓ Cached for reuse: {cache_file.name}")
        except Exception as e:
            print(f"  Warning: Failed to cache clouds: {e}")

        # Clean up old cache files
        try:
            cleanup_old_cache(cache_dir, days=cache_days)
        except:
            pass

        return img.convert("RGBA")

    except Exception as e:
        print(f"✗ Failed to download Matteason clouds: {e}")
        print(f"  If this persists, check https://clouds.matteason.co.uk")
        return None
def create_daynight_blend(day_img: Image.Image, night_img: Image.Image,
                          sun_lat: float, sun_lon: float,
                          blend_width: float = 6.0,
                          night_brightness: float = 0.5) -> Image.Image:
    """
    Blend day and night textures along the solar terminator.

    Args:
        day_img: Day texture (equirectangular projection)
        night_img: Night texture (equirectangular projection)
        sun_lat: Solar latitude in degrees
        sun_lon: Solar longitude in degrees
        blend_width: Width of the blend zone in degrees (default: 6.0)
        night_brightness: Brightness multiplier for night side (0.0-1.0, default: 0.5)

    Returns:
        Blended image
    """
    print(f"\nBlending day/night textures...")
    print(f"  Sun position: {sun_lat:.2f}°, {sun_lon:.2f}°")
    print(f"  Blend width: {blend_width}°")

    width, height = day_img.size

    # Convert to numpy arrays
    day_array = np.array(day_img, dtype=np.float32)
    night_array = np.array(night_img, dtype=np.float32)

    # Create blend mask based on terminator distance
    blend_array = np.zeros((height, width, 3), dtype=np.float32)

    for y in range(height):
        # Latitude from -90 to +90
        lat = 90 - (y / height) * 180

        for x in range(width):
            # Longitude from -180 to +180
            lon = (x / width) * 360 - 180

            # Calculate distance to terminator
            term_dist = calculate_terminator_distance(lat, lon, sun_lat, sun_lon)

            # Calculate blend factor (0 = night, 1 = day)
            # Smooth transition over blend_width degrees
            if term_dist > blend_width / 2:
                # Full day
                blend_factor = 1.0
            elif term_dist < -blend_width / 2:
                # Full night
                blend_factor = 0.0
            else:
                # Blend zone - use smooth cosine interpolation
                normalized = (term_dist + blend_width / 2) / blend_width
                blend_factor = (1 - math.cos(normalized * math.pi)) / 2

            # Blend pixel (darken night side but preserve bright city lights)
            night_pixel = night_array[y, x]

            # Preserve brightness of city lights (bright pixels)
            # For each channel, if it's bright (>40), keep it; otherwise darken it
            night_pixel_adjusted = np.where(
                night_pixel > 40,
                night_pixel,  # Keep bright lights at full brightness
                night_pixel * night_brightness  # Darken everything else
            )

            blend_array[y, x] = (
                blend_factor * day_array[y, x] +
                (1 - blend_factor) * night_pixel_adjusted
            )

    # Convert back to image
    blend_array = np.clip(blend_array, 0, 255).astype(np.uint8)
    blended_img = Image.fromarray(blend_array)

    print("✓ Blending complete")

    return blended_img


# =============================================================================
# Orthographic Projection
# =============================================================================

def reproject_to_orthographic(src_img: Image.Image,
                              center_lon: float,
                              center_lat: float,
                              output_size: int,
                              cloud_img: Image.Image = None,
                              cloud_opacity: float = 1.0,
                              sun_lat: float = 0.0,
                              sun_lon: float = 0.0) -> Image.Image:
    """
    Reproject an equirectangular image to orthographic (globe view).
    Optionally overlay clouds during reprojection to avoid polar artifacts.

    Args:
        src_img: Source image in equirectangular projection
        center_lon: Center longitude in degrees
        center_lat: Center latitude in degrees
        output_size: Output image size in pixels
        cloud_img: Optional cloud layer (equirectangular) to overlay during reprojection
        cloud_opacity: Opacity of cloud layer (0.0-1.0, default: 0.7)

    Returns:
        Orthographic projection image
    """
    print(f"\nReprojecting to orthographic view...")
    print(f"  Center: {center_lon:.2f}°, {center_lat:.2f}°")
    print(f"  Output size: {output_size}x{output_size}")

    src_width, src_height = src_img.size
    src_array = np.array(src_img)

    # Prepare cloud layer if provided
    cloud_array = None
    if cloud_img is not None:
        print(f"  Cloud overlay: enabled (opacity {cloud_opacity})")
        if cloud_img.size != src_img.size:
            cloud_img = cloud_img.resize(src_img.size, Image.Resampling.LANCZOS)
        cloud_array = np.array(cloud_img, dtype=np.float32)

    # Create output image
    output_array = np.zeros((output_size, output_size, 3), dtype=np.uint8)

    # Convert center to radians
    center_lon_rad = math.radians(center_lon)
    center_lat_rad = math.radians(center_lat)

    # Pre-compute trig values
    cos_center_lat = math.cos(center_lat_rad)
    sin_center_lat = math.sin(center_lat_rad)
    cos_center_lon = math.cos(center_lon_rad)
    sin_center_lon = math.sin(center_lon_rad)

    # Process each output pixel
    for y in range(output_size):
        for x in range(output_size):
            # Convert to normalized coordinates (-1 to 1)
            # px: -1 (left) to +1 (right)
            # py: +1 (top/north) to -1 (bottom/south) - inverted for north-up orientation
            px = (2 * x / output_size) - 1
            py = 1 - (2 * y / output_size)

            # Check if point is within unit circle
            rho = math.sqrt(px * px + py * py)
            if rho > 1.0:
                # Outside the Earth disk - pure black background
                output_array[y, x] = [0, 0, 0]  # Pure black
                continue

            # Orthographic inverse projection
            c = math.asin(rho)
            cos_c = math.cos(c)
            sin_c = math.sin(c)

            # Calculate latitude
            if rho == 0:
                lat = center_lat_rad
            else:
                lat = math.asin(cos_c * sin_center_lat + (py * sin_c * cos_center_lat / rho))

            # Calculate longitude
            if abs(cos_center_lat) < 1e-10:
                # Polar case
                lon = center_lon_rad
            else:
                lon = center_lon_rad + math.atan2(
                    px * sin_c,
                    rho * cos_center_lat * cos_c - py * sin_center_lat * sin_c
                )

            # Convert to degrees
            lat_deg = math.degrees(lat)
            lon_deg = math.degrees(lon)

            # Map to source image coordinates
            src_x = int((lon_deg + 180) / 360 * src_width) % src_width
            src_y = int((90 - lat_deg) / 180 * src_height)
            src_y = max(0, min(src_height - 1, src_y))

            # Sample base pixel
            base_pixel = src_array[src_y, src_x].astype(np.float32)

            # Apply cloud overlay if available (Matteason clouds)
            if cloud_array is not None:
                # Use bilinear interpolation for smoother cloud sampling
                src_x_frac = (lon_deg + 180) / 360 * src_width
                src_y_frac = (90 - lat_deg) / 180 * src_height

                # Clamp coordinates
                src_x_frac = max(0, min(src_width - 1.001, src_x_frac))
                src_y_frac = max(0, min(src_height - 1.001, src_y_frac))

                # Get integer and fractional parts
                x0 = int(src_x_frac)
                y0 = int(src_y_frac)
                x1 = min(x0 + 1, src_width - 1)
                y1 = min(y0 + 1, src_height - 1)

                fx = src_x_frac - x0
                fy = src_y_frac - y0

                # Bilinear interpolation of cloud data (RGBA)
                cloud_00 = cloud_array[y0, x0 % src_width]
                cloud_01 = cloud_array[y0, x1 % src_width]
                cloud_10 = cloud_array[y1, x0 % src_width]
                cloud_11 = cloud_array[y1, x1 % src_width]

                cloud_pixel = (
                    cloud_00 * (1 - fx) * (1 - fy) +
                    cloud_01 * fx * (1 - fy) +
                    cloud_10 * (1 - fx) * fy +
                    cloud_11 * fx * fy
                )

                # Matteason clouds: Alpha channel = cloud coverage (0-255)
                # Extract ONLY alpha channel (ignore RGB which has unwanted data)
                cloud_alpha_raw = cloud_pixel[3] / 255.0

                # Apply fluffy cloud rendering (very fluffy preset)
                # Threshold: filter out thin clouds
                threshold = 0.2
                cloud_alpha_raw = max(0, cloud_alpha_raw - threshold) / (1.0 - threshold) if cloud_alpha_raw > threshold else 0

                # Power curve for fluffiness (1.8 = very fluffy, dramatic clouds)
                cloud_alpha_raw = cloud_alpha_raw ** 1.8

                # Reduce cloud brightness on night side
                # Calculate terminator distance for this pixel
                term_dist = calculate_terminator_distance(lat_deg, lon_deg, sun_lat, sun_lon)

                # Day/night factor (0 = night, 1 = day)
                blend_width = 6.0
                if term_dist > blend_width / 2:
                    day_factor = 1.0
                elif term_dist < -blend_width / 2:
                    day_factor = 0.3  # Reduce clouds to 30% brightness on night side
                else:
                    normalized = (term_dist + blend_width / 2) / blend_width
                    day_factor = 0.3 + 0.7 * ((1 - math.cos(normalized * math.pi)) / 2)

                # Apply final opacity with day/night reduction
                cloud_alpha = cloud_alpha_raw * cloud_opacity * day_factor

                # Blend: pure white clouds overlay on base image
                # result = (1 - alpha) * base + alpha * white
                white_cloud = 255.0
                base_pixel = (1 - cloud_alpha) * base_pixel + cloud_alpha * white_cloud

            output_array[y, x] = np.clip(base_pixel, 0, 255).astype(np.uint8)

    print("✓ Reprojection complete")

    return Image.fromarray(output_array)


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate full-disk Earth view with day/night blending",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--center",
        nargs=2,
        type=float,
        default=[10.0, 55.0],
        metavar=("LON", "LAT"),
        help="Center longitude and latitude (default: 10.0 55.0 for Northern Europe)"
    )
    parser.add_argument(
        "--size",
        type=int,
        default=2048,
        help="Output image size in pixels (default: 2048)"
    )
    parser.add_argument(
        "--time",
        type=str,
        help="UTC time for sun position (default: current time, format: 'YYYY-MM-DD HH:MM:SS')"
    )
    parser.add_argument(
        "--blend-width",
        type=float,
        default=6.0,
        help="Width of day/night blend zone in degrees (default: 6.0)"
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Output file path (default: earth_daynight_TIMESTAMP.png)"
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "diy-xplanet",
        help="Directory for cached texture files"
    )
    parser.add_argument(
        "--no-reproject",
        action="store_true",
        help="Skip reprojection, output equirectangular blend only"
    )
    parser.add_argument(
        "--clouds",
        action="store_true",
        help="Overlay real-time cloud data from NASA GIBS or NOAA GMGSI"
    )
    parser.add_argument(
        "--cloud-opacity",
        type=float,
        default=0.7,
        help="Cloud layer opacity (0.0-1.0, default: 0.7)"
    )
    parser.add_argument(
        "--combine-modis",
        action="store_true",
        help="Combine MODIS Terra + Aqua to fill orbital swath gaps (slower but better coverage)"
    )
    parser.add_argument(
        "--hybrid-polar",
        action="store_true",
        help="Use VIIRS for global coverage and fill polar gaps with GMGSI (recommended)"
    )
    parser.add_argument(
        "--canvas-aspect",
        type=float,
        default=0.0,
        help="Pad the output onto a black canvas of this width/height ratio (e.g. 1.778 "
             "for 16:9). 0 = no padding, keep it square. generate_latest_wallpaper.sh "
             "detects your widest display and passes it automatically."
    )
    parser.add_argument(
        "--keep-outputs",
        type=int,
        default=0,
        help="Number of generated images to keep in output/final (0 = keep all, the default; "
             "generate_latest_wallpaper.sh passes an explicit value)"
    )
    parser.add_argument(
        "--cache-days",
        type=int,
        default=2,
        help="Days of downloaded cloud data to keep in the cache (default: 2)"
    )

    args = parser.parse_args()

    # Parse time
    if args.time:
        try:
            dt = datetime.strptime(args.time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Error: Invalid time format. Use 'YYYY-MM-DD HH:MM:SS'", file=sys.stderr)
            sys.exit(1)
    else:
        dt = datetime.now(timezone.utc)

    print(f"Generating Earth view for: {dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"Center: {args.center[0]:.2f}°, {args.center[1]:.2f}°\n")

    # Calculate sun position
    sun_lat, sun_lon = calculate_sun_position(dt)
    print(f"Solar position (subsolar point): {sun_lat:.2f}°, {sun_lon:.2f}°\n")

    # Determine base image path (cached day/night blend without clouds)
    output_dir = Path("output/final")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename based on month, day, hour (not minutes/seconds)
    year_month = dt.strftime('%Y-%m')  # e.g., "2026-01"
    day_hour = dt.strftime('%d-%Hh')    # e.g., "22-13h"

    # Base image (day/night blend, no clouds) - this gets cached
    base_image_path = output_dir / f"earth_{year_month}_{day_hour}_base.png"

    # Check if we need to generate the base image
    need_base_generation = not base_image_path.exists()

    if need_base_generation:
        print(f"\nGenerating base image for {dt.strftime('%B %d, %Y at %H:00 UTC')}...")

    # Generate or load the base image (day/night blend without clouds)
    if need_base_generation:
        # Load textures (use current month for seasonal Blue Marble texture)
        current_month = dt.month
        day_img, night_img = load_and_prepare_textures(args.cache_dir, current_month)

        # Blend day and night
        blended = create_daynight_blend(day_img, night_img, sun_lat, sun_lon, args.blend_width)

        # Reproject to orthographic (no clouds yet)
        if args.no_reproject:
            print("\nSkipping reprojection (--no-reproject)")
            base_output = blended
        else:
            base_output = reproject_to_orthographic(
                blended, args.center[0], args.center[1], args.size,
                cloud_img=None, cloud_opacity=0,  # No clouds in base image
                sun_lat=sun_lat, sun_lon=sun_lon
            )

        # Save base image
        print(f"\nSaving base image to: {base_image_path}")
        base_output.save(base_image_path, "PNG")
        print(f"✓ Saved base ({base_image_path.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        # Load existing base image
        print(f"\n✓ Using cached base image: {base_image_path}")
        print(f"  Size: {base_image_path.stat().st_size / 1024 / 1024:.1f} MB")
        base_output = Image.open(base_image_path)

    # Now handle cloud overlay if requested
    if args.clouds:
        print("\nApplying fresh cloud overlay...")

        # Download current cloud layer (always fresh, never cached final result)
        cloud_img = download_cloud_layer(
            size=5400,  # Use source resolution for clouds
            combine_modis=args.combine_modis,
            hybrid_polar=args.hybrid_polar,
            cache_dir=args.cache_dir,
            cache_days=args.cache_days
        )

        if cloud_img is not None:
            # Apply clouds to the base image
            if args.no_reproject:
                # For equirectangular output, apply clouds here
                print("\nOverlaying clouds on equirectangular image...")
                if cloud_img.size != base_output.size:
                    cloud_img = cloud_img.resize(base_output.size, Image.Resampling.LANCZOS)
                base_array = np.array(base_output, dtype=np.float32)
                cloud_array = np.array(cloud_img, dtype=np.float32)
                cloud_brightness = np.mean(cloud_array, axis=2)
                cloud_alpha = (cloud_brightness / 255.0) * args.cloud_opacity
                for c in range(3):
                    base_array[:, :, c] = (1 - cloud_alpha) * base_array[:, :, c] + cloud_alpha * cloud_array[:, :, c]
                final_output = Image.fromarray(np.clip(base_array, 0, 255).astype(np.uint8))
                print("✓ Cloud overlay complete")
            else:
                # For orthographic, we need to re-project with clouds
                # Load the blended equirectangular to apply clouds during reprojection
                current_month = dt.month
                day_img, night_img = load_and_prepare_textures(args.cache_dir, current_month)
                blended = create_daynight_blend(day_img, night_img, sun_lat, sun_lon, args.blend_width)

                final_output = reproject_to_orthographic(
                    blended, args.center[0], args.center[1], args.size,
                    cloud_img=cloud_img, cloud_opacity=args.cloud_opacity,
                    sun_lat=sun_lat, sun_lon=sun_lon
                )
        else:
            print("  Warning: Cloud download failed, using base image only")
            final_output = base_output

        # Determine final output path with timestamp (clouds change, so include time)
        timestamp = dt.strftime('%Y%m%d_%H%M%S')
        final_output_path = output_dir / f"earth_{year_month}_{day_hour}_clouds_{timestamp}.png"
    else:
        # No clouds requested, use base image as final
        final_output = base_output
        final_output_path = base_image_path

    # Pad for the screen. The square base image stays untouched on disk so it
    # remains reusable as a cache; a padded no-clouds render gets its own name.
    if args.canvas_aspect > 0:
        padded = fit_to_canvas(final_output, args.canvas_aspect)
        if padded is not final_output:
            final_output = padded
            if not args.clouds:
                final_output_path = output_dir / f"earth_{year_month}_{day_hour}_fit.png"
                final_output.save(final_output_path, "PNG")
                print(f"\u2713 Saved ({final_output_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # Save final output (only if different from base)
    if args.clouds:
        print(f"\nSaving final image with clouds to: {final_output_path}")
        final_output.save(final_output_path, "PNG")
        print(f"✓ Saved ({final_output_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # Create symlink to latest in root directory
    latest_link = Path("earth_daynight_latest.png")
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    latest_link.symlink_to(final_output_path)
    print(f"✓ Symlink updated: {latest_link} -> {final_output_path}")

    prune_outputs(output_dir, args.keep_outputs, protect=final_output_path)

    print("\n✓ Done!")


if __name__ == "__main__":
    main()
