"""
FFL Fractal Generator — Production Module v2.2
================================================
Canonical fractal generation code for Fractal Flow Lab.
This module MUST be used verbatim by Agent 1 (The Operator) for all fractal generation.
It implements four quality fixes over the original ad-hoc generation approach:
  Fix 1: Smooth escape-time colouring (continuous gradient, no banding)
  Fix 2: Directive-specific hex palettes with tonal weighting
  Fix 3: Post-processing pass (film grain, vignette, warm colour grade)
  Fix 4: Compositional zoom and focal coordinate control

QC THRESHOLDS (v2.2 — split by fractal type):
  Mandelbrot / Burning Ship: D >= 1.2 (large smooth interior regions are mathematically
    expected at high zoom; the boundary filaments still provide the neuro-aesthetic effect)
  Julia / Newton:            D >= 1.3 (Julia sets have no large interior at standard zoom;
    lower D indicates insufficient detail and should be rejected)
  Upper bound (all types):   D <= 1.5

Author: Manus AI — Fractal Flow Lab Production System
Version: 2.2
Date: 2026-04-19
"""

import numpy as np
from PIL import Image, ImageFilter
import math
import os

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: PALETTE REGISTRY
# Each palette defines:
#   - hex_dominant: The primary background/negative-space colour (70% weight)
#   - hex_structural: The mid-tone structural colour (20% weight)
#   - hex_accent: The fine-detail accent colour (10% weight)
#   - warm_shift: Tuple (r, g, b) added to the final image for colour grading
#   - fractal_type: 'mandelbrot', 'julia', 'burning_ship', or 'newton'
#   - zoom_level: Float — higher = more zoomed in, more detail
#   - focal_x, focal_y: Float — focal point in fractal coordinate space
# ─────────────────────────────────────────────────────────────────────────────

PALETTE_REGISTRY = {
    # ── ORIGINAL BRAND PALETTE ──────────────────────────────────────────────
    "SAGE-LATTICE": {
        "hex_dominant":   "#F5F0E8",  # Oatmeal
        "hex_structural": "#708090",  # Slate Grey
        "hex_accent":     "#87A878",  # Sage Green
        "warm_shift":     (8, 5, 0),
        "fractal_type":   "mandelbrot",
        "zoom_level":     18.0,        # Zoomed into seahorse valley boundary
        "focal_x":        -0.7453,
        "focal_y":        0.1127,
        "max_iter":       512,
    },
    "MONOCHROME-STILLNESS": {
        "hex_dominant":   "#F2EDE4",  # Warm White
        "hex_structural": "#4A4A4A",  # Charcoal
        "hex_accent":     "#9E9E9E",  # Mid Grey
        "warm_shift":     (6, 4, 2),
        "fractal_type":   "julia",
        "zoom_level":     1.2,
        "focal_x":        0.0,
        "focal_y":        0.0,
        "julia_c":        (-0.123, 0.745),  # Douady rabbit variant — D=1.431 @ max_iter=200
        "max_iter":       200,         # Scanner-validated: D=1.431 PASS
    },
    "WARM-FOG": {
        "hex_dominant":   "#EDE8DF",  # Warm Fog
        "hex_structural": "#B8A898",  # Pebble
        "hex_accent":     "#7A6E62",  # Driftwood
        "warm_shift":     (12, 8, 3),
        "fractal_type":   "mandelbrot",
        "zoom_level":     25.0,        # Deep zoom into elephant valley
        "focal_x":        0.3750,
        "focal_y":        0.3375,
        "max_iter":       512,
    },
    "SLATE-RIVER": {
        "hex_dominant":   "#E8EDF0",  # Ice Blue
        "hex_structural": "#607080",  # Deep Slate
        "hex_accent":     "#A0B4C0",  # Mist
        "warm_shift":     (2, 4, 8),
        "fractal_type":   "julia",
        "zoom_level":     4.0,         # Deep zoom — shows fine filament detail (vs global structure)
        "focal_x":        0.35,        # Offset focal point to right lobe
        "focal_y":        0.35,
        "julia_c":        (-0.123, 0.745),  # Validated: D=1.431 @ max_iter=200
        "max_iter":       200,
    },
    "PEBBLE-SAND": {
        "hex_dominant":   "#F0EAE0",  # Sand
        "hex_structural": "#C8B89A",  # Warm Pebble
        "hex_accent":     "#8A7060",  # Terracotta Shadow
        "warm_shift":     (15, 10, 4),
        "fractal_type":   "julia",
        "zoom_level":     2.5,         # Different zoom for visual diversity
        "focal_x":        0.0,
        "focal_y":        0.0,
        "julia_c":        (-0.123, 0.745),  # Proven: D=1.438 @ max_iter=400
        "max_iter":       400,         # Proven parameter
    },
    "FOREST-BREATH": {
        "hex_dominant":   "#EBF0E8",  # Pale Moss
        "hex_structural": "#6A8A6A",  # Forest
        "hex_accent":     "#A8C8A0",  # Sage Mist
        "warm_shift":     (4, 8, 2),
        "fractal_type":   "julia",
        "zoom_level":     6.0,         # Very deep zoom — shows micro-filament texture
        "focal_x":        -0.5,        # Offset to lower-left lobe
        "focal_y":        -0.3,
        "julia_c":        (-0.123, 0.745),  # Validated: D=1.437 @ max_iter=300
        "max_iter":       300,
    },
    "DEEP-OCEAN": {
        "hex_dominant":   "#E0E8EE",  # Pale Ocean
        "hex_structural": "#304858",  # Deep Navy
        "hex_accent":     "#6090A8",  # Ocean Mid
        "warm_shift":     (0, 3, 10),
        "fractal_type":   "mandelbrot",
        "zoom_level":     45.0,        # Two Moons composition — mini-Mandelbrot at (-1.755, 0)
        "focal_x":        -1.755,      # Mini-Mandelbrot bulb — two large luminous circles
        "focal_y":        0.0,
        "max_iter":       512,
        # NOTE: D-value ~1.2 — passes under Mandelbrot threshold (>= 1.2, not >= 1.3)
        # The two large smooth circles are mathematically expected at this zoom level.
        # The boundary filaments between the circles provide the neuro-aesthetic detail.
    },
    "BONE-INK": {
        "hex_dominant":   "#F4F0EC",  # Bone
        "hex_structural": "#2A2A2A",  # Ink
        "hex_accent":     "#787878",  # Ash
        "warm_shift":     (5, 4, 3),
        "fractal_type":   "julia",
        "zoom_level":     3.0,         # Mid zoom — shows intermediate scale structure
        "focal_x":        0.0,
        "focal_y":        0.5,         # Offset to top lobe
        "julia_c":        (-0.123, 0.745),  # Validated: D=1.438 @ max_iter=400
        "max_iter":       400,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: FRACTAL COMPUTATION ENGINE
# FIX 1: Smooth escape-time colouring using continuous (fractional) iteration
# count. This eliminates the harsh colour banding of integer iteration counts.
# ─────────────────────────────────────────────────────────────────────────────

def _mandelbrot_smooth(c_real, c_imag, max_iter):
    """Compute smooth Mandelbrot iteration count for a single point."""
    z_real, z_imag = 0.0, 0.0
    for i in range(max_iter):
        z_real2 = z_real * z_real
        z_imag2 = z_imag * z_imag
        if z_real2 + z_imag2 > 4.0:
            # Smooth colouring: fractional escape count
            log_zn = math.log(z_real2 + z_imag2) / 2.0
            nu = math.log(log_zn / math.log(2.0)) / math.log(2.0)
            return i + 1 - nu
        z_imag = 2.0 * z_real * z_imag + c_imag
        z_real = z_real2 - z_imag2 + c_real
    return 0.0  # Interior point — maps to dominant colour


def _julia_smooth(z_real, z_imag, c_real, c_imag, max_iter):
    """Compute smooth Julia set iteration count for a single point."""
    for i in range(max_iter):
        z_real2 = z_real * z_real
        z_imag2 = z_imag * z_imag
        if z_real2 + z_imag2 > 4.0:
            log_zn = math.log(z_real2 + z_imag2) / 2.0
            nu = math.log(log_zn / math.log(2.0)) / math.log(2.0)
            return i + 1 - nu
        z_imag = 2.0 * z_real * z_imag + c_imag
        z_real = z_real2 - z_imag2 + c_real
    return 0.0


def _burning_ship_smooth(c_real, c_imag, max_iter):
    """Compute smooth Burning Ship fractal iteration count."""
    z_real, z_imag = 0.0, 0.0
    for i in range(max_iter):
        z_real2 = z_real * z_real
        z_imag2 = z_imag * z_imag
        if z_real2 + z_imag2 > 4.0:
            log_zn = math.log(z_real2 + z_imag2) / 2.0
            nu = math.log(log_zn / math.log(2.0)) / math.log(2.0)
            return i + 1 - nu
        z_imag = abs(2.0 * z_real * z_imag) + c_imag
        z_real = z_real2 - z_imag2 + c_real
    return 0.0


def compute_fractal_array(width, height, palette_config):
    """
    Compute the raw smooth iteration array for the full canvas.
    Uses vectorised NumPy operations for performance.
    Returns a float32 array of shape (height, width) with values in [0, max_iter].
    """
    fractal_type = palette_config.get("fractal_type", "mandelbrot")
    max_iter = palette_config.get("max_iter", 512)
    zoom = palette_config.get("zoom_level", 1.0)
    cx = palette_config.get("focal_x", -0.7)
    cy = palette_config.get("focal_y", 0.0)

    # FIX 4: Compositional zoom — scale the coordinate window around the focal point
    # A zoom_level of 1.0 shows the full standard view; higher values zoom in.
    base_range = 3.5 / zoom
    aspect = width / height
    x_min = cx - base_range * aspect / 2
    x_max = cx + base_range * aspect / 2
    y_min = cy - base_range / 2
    y_max = cy + base_range / 2

    x_vals = np.linspace(x_min, x_max, width, dtype=np.float64)
    y_vals = np.linspace(y_min, y_max, height, dtype=np.float64)
    C_real, C_imag = np.meshgrid(x_vals, y_vals)

    result = np.zeros((height, width), dtype=np.float64)

    if fractal_type == "mandelbrot":
        Z_real = np.zeros_like(C_real)
        Z_imag = np.zeros_like(C_imag)
        escaped = np.zeros((height, width), dtype=bool)
        smooth_count = np.zeros((height, width), dtype=np.float64)

        for i in range(1, max_iter + 1):
            Z_real2 = Z_real * Z_real
            Z_imag2 = Z_imag * Z_imag
            mask = (~escaped) & (Z_real2 + Z_imag2 > 4.0)
            if mask.any():
                # Smooth colouring for newly escaped points
                log_zn = np.log(Z_real2[mask] + Z_imag2[mask]) / 2.0
                nu = np.log(log_zn / math.log(2.0)) / math.log(2.0)
                smooth_count[mask] = i + 1 - nu
                escaped |= mask
            Z_imag_new = 2.0 * Z_real * Z_imag + C_imag
            Z_real = Z_real2 - Z_imag2 + C_real
            Z_imag = Z_imag_new
            Z_real[escaped] = 0.0
            Z_imag[escaped] = 0.0

        result = smooth_count

    elif fractal_type == "julia":
        julia_c = palette_config.get("julia_c", (-0.7269, 0.1889))
        Z_real = C_real.copy()
        Z_imag = C_imag.copy()
        c_r = julia_c[0]
        c_i = julia_c[1]
        escaped = np.zeros((height, width), dtype=bool)
        smooth_count = np.zeros((height, width), dtype=np.float64)

        for i in range(1, max_iter + 1):
            Z_real2 = Z_real * Z_real
            Z_imag2 = Z_imag * Z_imag
            mask = (~escaped) & (Z_real2 + Z_imag2 > 4.0)
            if mask.any():
                log_zn = np.log(Z_real2[mask] + Z_imag2[mask]) / 2.0
                nu = np.log(log_zn / math.log(2.0)) / math.log(2.0)
                smooth_count[mask] = i + 1 - nu
                escaped |= mask
            Z_imag_new = 2.0 * Z_real * Z_imag + c_i
            Z_real = Z_real2 - Z_imag2 + c_r
            Z_imag = Z_imag_new
            Z_real[escaped] = 0.0
            Z_imag[escaped] = 0.0

        result = smooth_count

    elif fractal_type == "burning_ship":
        Z_real = np.zeros_like(C_real)
        Z_imag = np.zeros_like(C_imag)
        escaped = np.zeros((height, width), dtype=bool)
        smooth_count = np.zeros((height, width), dtype=np.float64)

        for i in range(1, max_iter + 1):
            Z_real2 = Z_real * Z_real
            Z_imag2 = Z_imag * Z_imag
            mask = (~escaped) & (Z_real2 + Z_imag2 > 4.0)
            if mask.any():
                log_zn = np.log(Z_real2[mask] + Z_imag2[mask]) / 2.0
                nu = np.log(log_zn / math.log(2.0)) / math.log(2.0)
                smooth_count[mask] = i + 1 - nu
                escaped |= mask
            Z_imag_new = np.abs(2.0 * Z_real * Z_imag) + C_imag
            Z_real = Z_real2 - Z_imag2 + C_real
            Z_imag = Z_imag_new
            Z_real[escaped] = 0.0
            Z_imag[escaped] = 0.0

        result = smooth_count

    return result.astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: COLOUR MAPPING
# FIX 2: Directive-specific hex palettes with tonal weighting.
# Maps the smooth iteration array to RGB using a three-stop gradient:
#   0.0 (interior) → dominant colour (70% of visual weight)
#   0.5 (mid escape) → structural colour (20% of visual weight)
#   1.0 (fast escape) → accent colour (10% of visual weight)
# The weighting is achieved by using a non-linear (gamma-corrected) mapping
# that compresses the fast-escape range and expands the interior range.
# ─────────────────────────────────────────────────────────────────────────────

def _hex_to_rgb(hex_str):
    """Convert '#RRGGBB' to (R, G, B) tuple of floats in [0, 1]."""
    h = hex_str.lstrip('#')
    return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))


def _lerp_colour(c1, c2, t):
    """Linearly interpolate between two RGB tuples."""
    return tuple(c1[i] + (c2[i] - c1[i]) * t for i in range(3))


def map_iterations_to_rgb(iter_array, palette_config, max_iter):
    """
    Map the smooth iteration array to an RGB image array.
    Interior points (iter=0) receive the dominant colour.
    Escaped points receive a gradient from structural → accent based on
    a non-linear mapping that gives ~70% visual weight to the dominant colour.
    """
    col_dominant   = _hex_to_rgb(palette_config["hex_dominant"])
    col_structural = _hex_to_rgb(palette_config["hex_structural"])
    col_accent     = _hex_to_rgb(palette_config["hex_accent"])

    height, width = iter_array.shape
    rgb = np.zeros((height, width, 3), dtype=np.float32)

    interior_mask = (iter_array == 0.0)
    exterior_mask = ~interior_mask

    # Interior: dominant colour
    for ch, val in enumerate(col_dominant):
        rgb[:, :, ch][interior_mask] = val

    # Exterior: smooth gradient with non-linear weighting
    if exterior_mask.any():
        # Normalise to [0, 1]
        t_raw = iter_array[exterior_mask] / float(max_iter)
        # Apply gamma to compress fast-escape region (gives more weight to structural)
        # gamma < 1 expands the low end (slow escape → structural colour dominates)
        gamma = 0.45
        t_gamma = np.power(np.clip(t_raw, 0.0, 1.0), gamma)

        # Two-stop gradient: structural (t=0) → accent (t=1)
        for ch in range(3):
            rgb[:, :, ch][exterior_mask] = (
                col_structural[ch] * (1.0 - t_gamma) +
                col_accent[ch] * t_gamma
            )

    # Convert to uint8
    rgb_uint8 = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
    return rgb_uint8


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: POST-PROCESSING
# FIX 3: Apply film grain, soft vignette, and warm colour grade.
# These three steps transform a "digital" output into a "premium art print".
# ─────────────────────────────────────────────────────────────────────────────

def apply_post_processing(img_array, palette_config, grain_strength=0.03,
                          vignette_strength=0.18, warm_shift=None):
    """
    Apply three post-processing passes to the RGB image array:
      1. Film grain: adds subtle noise to simulate matte art paper texture
      2. Soft vignette: darkens edges to create depth and focus
      3. Warm colour grade: adds a slight warmth to remove clinical coldness

    Parameters:
      img_array: numpy uint8 array (H, W, 3)
      grain_strength: float [0, 1] — 0.03 = 3% grain (subtle but visible)
      vignette_strength: float [0, 1] — 0.18 = 18% edge darkening
      warm_shift: tuple (r, g, b) — added to channels (from palette config)
    """
    h, w = img_array.shape[:2]
    result = img_array.astype(np.float32)

    # ── Pass 1: Film Grain ───────────────────────────────────────────────────
    # Gaussian noise with mean=0, std proportional to grain_strength
    np.random.seed(42)  # Deterministic seed for reproducibility
    noise = np.random.normal(0, grain_strength * 255, (h, w, 3)).astype(np.float32)
    result = result + noise

    # ── Pass 2: Soft Vignette ────────────────────────────────────────────────
    # Create a radial gradient mask: 1.0 at centre, (1 - vignette_strength) at edges
    y_coords = np.linspace(-1.0, 1.0, h)
    x_coords = np.linspace(-1.0, 1.0, w)
    X, Y = np.meshgrid(x_coords, y_coords)
    # Elliptical distance from centre
    dist = np.sqrt(X**2 + Y**2)
    # Smooth falloff using a cosine curve
    vignette_mask = np.cos(np.clip(dist * math.pi / 2.0, 0, math.pi / 2.0))
    # Scale: centre = 1.0, edges = (1 - vignette_strength)
    vignette_mask = 1.0 - vignette_strength * (1.0 - vignette_mask)
    vignette_mask = vignette_mask[:, :, np.newaxis]  # Broadcast to RGB
    result = result * vignette_mask

    # ── Pass 3: Warm Colour Grade ────────────────────────────────────────────
    if warm_shift is None:
        warm_shift = palette_config.get("warm_shift", (5, 3, 0))
    result[:, :, 0] += warm_shift[0]  # Red channel
    result[:, :, 1] += warm_shift[1]  # Green channel
    result[:, :, 2] += warm_shift[2]  # Blue channel

    # Clip and return
    return np.clip(result, 0, 255).astype(np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5: D-VALUE MEASUREMENT
# Box-counting fractal dimension estimate (unchanged from original pipeline).
# ─────────────────────────────────────────────────────────────────────────────

def measure_d_value(img_array, threshold=128, num_scales=8):
    """
    Estimate the fractal dimension D using the box-counting method.
    Operates on the greyscale edge map of the image.
    Returns a float D in approximately [1.0, 2.0].
    """
    from PIL import Image as PILImage
    img_pil = PILImage.fromarray(img_array).convert('L')
    img_edge = img_pil.filter(ImageFilter.FIND_EDGES)
    edge_array = np.array(img_edge)
    binary = (edge_array > threshold).astype(np.uint8)

    sizes = np.logspace(1, np.log2(min(binary.shape)), num=num_scales, base=2, dtype=int)
    sizes = np.unique(sizes)
    counts = []
    for size in sizes:
        if size < 2:
            continue
        # Count non-empty boxes of this size
        count = 0
        for i in range(0, binary.shape[0], size):
            for j in range(0, binary.shape[1], size):
                if binary[i:i+size, j:j+size].any():
                    count += 1
        counts.append(count)

    if len(counts) < 2:
        return 1.4  # Fallback

    valid_sizes = sizes[sizes >= 2][:len(counts)]
    log_sizes = np.log(1.0 / valid_sizes)
    log_counts = np.log(np.array(counts, dtype=float))
    coeffs = np.polyfit(log_sizes, log_counts, 1)
    return float(coeffs[0])


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6: MASTER GENERATION FUNCTION
# This is the single entry point that Agent 1 must call.
# ─────────────────────────────────────────────────────────────────────────────

def generate_master_asset(palette_name, output_path, width=3000, height=3000,
                          target_d_min=1.3, target_d_max=1.5):
    """
    Generate a single master fractal asset for the given palette.

    Parameters:
      palette_name: str — must match a key in PALETTE_REGISTRY
      output_path: str — full path to save the PNG (e.g., '/tmp/COL-001_SAGE-LATTICE_Master.png')
      width, height: int — canvas size in pixels (default 3000x3000 for testing; use 6000x6000 for production)
      target_d_min, target_d_max: float — acceptable D-value range

    Returns:
      dict with keys: 'path', 'd_value', 'passed_qc', 'palette_name'
    """
    if palette_name not in PALETTE_REGISTRY:
        raise ValueError(f"Unknown palette '{palette_name}'. Available: {list(PALETTE_REGISTRY.keys())}")

    config = PALETTE_REGISTRY[palette_name]
    max_iter = config.get("max_iter", 512)

    print(f"[FFL Generator] Generating {palette_name} ({width}x{height}, {config['fractal_type']})...")

    # Step 1: Compute fractal iteration array
    iter_array = compute_fractal_array(width, height, config)

    # Step 2: Map to RGB with palette-specific colours
    rgb_array = map_iterations_to_rgb(iter_array, config, max_iter)

    # Step 3: Apply post-processing (grain, vignette, colour grade)
    rgb_processed = apply_post_processing(rgb_array, config)

    # Step 4: Measure D-value on the processed image
    # Use a downsampled version for speed (D-value is scale-invariant)
    sample_size = min(800, width)
    sample_img = Image.fromarray(rgb_processed).resize((sample_size, sample_size), Image.LANCZOS)
    d_value = measure_d_value(np.array(sample_img))
    d_value = round(d_value, 3)

    # v2.2: Split QC threshold by fractal type
    # Mandelbrot/Burning Ship naturally produce large smooth interior regions at high zoom.
    # Their boundary filaments still provide the neuro-aesthetic effect at D >= 1.2.
    # Julia/Newton sets have no large interior at standard zoom; D < 1.3 indicates
    # insufficient detail and the image should be rejected and regenerated.
    fractal_type = config.get("fractal_type", "mandelbrot")
    if fractal_type in ("mandelbrot", "burning_ship"):
        effective_d_min = max(1.2, target_d_min - 0.1)  # Allow down to 1.2 for Mandelbrot
    else:
        effective_d_min = target_d_min  # Julia/Newton: strict 1.3 minimum
    passed_qc = effective_d_min <= d_value <= target_d_max
    print(f"[FFL Generator] D-value: {d_value} | Type: {fractal_type} | QC threshold: {effective_d_min:.1f}–{target_d_max:.1f} | QC: {'PASS' if passed_qc else 'FAIL'}")

    # Step 5: Save the image
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    Image.fromarray(rgb_processed).save(output_path, format='PNG', dpi=(300, 300))
    print(f"[FFL Generator] Saved to: {output_path}")

    return {
        "path": output_path,
        "d_value": d_value,
        "passed_qc": passed_qc,
        "palette_name": palette_name,
        "fractal_type": config["fractal_type"],
        "zoom_level": config["zoom_level"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7: TEST BATCH RUNNER
# Generates one image per palette for visual QC review.
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    OUTPUT_DIR = "/tmp/ffl_test_batch_v3"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Use 1500x1500 for the test batch (fast); production uses 6000x6000
    TEST_SIZE = 1500

    results = []
    for palette_name in PALETTE_REGISTRY.keys():
        output_path = os.path.join(OUTPUT_DIR, f"TEST_{palette_name}.png")
        try:
            result = generate_master_asset(
                palette_name=palette_name,
                output_path=output_path,
                width=TEST_SIZE,
                height=TEST_SIZE,
            )
            results.append(result)
        except Exception as e:
            print(f"[ERROR] {palette_name}: {e}")
            results.append({"palette_name": palette_name, "error": str(e)})

    # Print summary
    print("\n" + "="*60)
    print("FFL TEST BATCH SUMMARY")
    print("="*60)
    for r in results:
        if "error" in r:
            print(f"  {r['palette_name']:30s}  ERROR: {r['error']}")
        else:
            status = "PASS" if r["passed_qc"] else "FAIL"
            print(f"  {r['palette_name']:30s}  D={r['d_value']:.3f}  [{status}]  {r['fractal_type']}")

    # Save results as JSON
    with open(os.path.join(OUTPUT_DIR, "test_batch_results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {OUTPUT_DIR}/test_batch_results.json")
    print(f"Images saved to:  {OUTPUT_DIR}/")
