"""
FFL Similarity Checker — Production Module v1.0
================================================
Perceptual similarity rejection system for Fractal Flow Lab.
Agent 1 MUST call check_similarity() before accepting any generated asset.

HOW IT WORKS:
  1. Each accepted image is hashed using pHash (perceptual hash) — a 64-bit fingerprint
     that captures the visual structure of the image, not its pixel values.
  2. When a new image is generated, its pHash is compared against all previously
     accepted hashes stored in the local similarity index.
  3. If the Hamming distance between the new hash and any existing hash is <= REJECT_THRESHOLD,
     the image is rejected as "too similar" and Agent 1 must regenerate with different parameters.
  4. If accepted, the new hash is added to the index for future comparisons.

THRESHOLD CALIBRATION:
  Hamming distance is the number of bits that differ between two 64-bit hashes.
  - Distance 0:   Identical images
  - Distance 1–5: Near-identical (different noise/grain only)
  - Distance 6–10: Very similar composition and colour
  - Distance 11–15: Similar but with noticeable differences
  - Distance 16+: Clearly different images

  REJECT_THRESHOLD = 10 (reject if distance <= 10, i.e., "very similar or identical")
  This is conservative — it will catch same-composition/different-palette pairs.
  Adjust to 12–14 if you want to allow more colour variation on the same composition.

INDEX STORAGE:
  Hashes are stored in a JSON file at FFL_SIMILARITY_INDEX_PATH.
  Each entry: {"palette_name": str, "directive_id": str, "phash": str, "path": str, "date": str}
  The index is append-only — hashes are never deleted (to prevent re-generation of rejected images).

Author: Manus AI — Fractal Flow Lab Production System
Version: 1.0
Date: 2026-04-19
"""

import json
import os
from datetime import datetime
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

# Path to the persistent similarity index JSON file
# Agent 1 should set this to a path inside the Google Drive sync folder
# so the index persists across sessions.
FFL_SIMILARITY_INDEX_PATH = os.environ.get(
    "FFL_SIMILARITY_INDEX_PATH",
    os.path.expanduser("~/ffl_similarity_index.json")
)

# Hamming distance threshold — images with distance <= this are rejected as too similar
# 12 allows intentional zoom-based variations on the same Julia formula (distance ~10–11)
# while still catching near-identical images (distance <= 10 = very similar composition)
REJECT_THRESHOLD = 12

# Hash size — 8 = 64-bit pHash (standard), 16 = 256-bit (more sensitive)
HASH_SIZE = 8


# ─────────────────────────────────────────────────────────────────────────────
# INDEX MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def load_index(index_path=None):
    """Load the similarity index from disk. Returns an empty list if not found."""
    path = index_path or FFL_SIMILARITY_INDEX_PATH
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return json.load(f)


def save_index(index, index_path=None):
    """Save the similarity index to disk."""
    path = index_path or FFL_SIMILARITY_INDEX_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(index, f, indent=2)


def add_to_index(palette_name, directive_id, image_path, index_path=None):
    """
    Compute the pHash for the image at image_path and add it to the index.
    Call this ONLY after an image has passed all QC checks and been accepted.
    """
    img = Image.open(image_path).convert("RGB")
    phash = str(imagehash.phash(img, hash_size=HASH_SIZE))

    index = load_index(index_path)
    index.append({
        "palette_name": palette_name,
        "directive_id": directive_id,
        "phash": phash,
        "path": image_path,
        "date": datetime.now().isoformat()[:10],
    })
    save_index(index, index_path)
    print(f"[FFL Similarity] Added to index: {palette_name} ({directive_id}) — pHash: {phash}")
    return phash


# ─────────────────────────────────────────────────────────────────────────────
# SIMILARITY CHECK
# ─────────────────────────────────────────────────────────────────────────────

def check_similarity(image_path_or_array, reject_threshold=None, index_path=None):
    """
    Check whether a new image is too similar to any previously accepted image.

    Parameters:
      image_path_or_array: str (file path) or numpy array (H, W, 3)
      reject_threshold: int — Hamming distance threshold (default: REJECT_THRESHOLD)
      index_path: str — path to the similarity index JSON (default: FFL_SIMILARITY_INDEX_PATH)

    Returns:
      dict with keys:
        'passed':       bool — True if the image is sufficiently different from all existing images
        'phash':        str  — pHash of the new image
        'closest_match': dict or None — the most similar existing entry (if any)
        'min_distance': int  — Hamming distance to the closest match (0 = identical)
        'threshold':    int  — the threshold used
    """
    threshold = reject_threshold if reject_threshold is not None else REJECT_THRESHOLD

    # Compute pHash of the new image
    if isinstance(image_path_or_array, (str, Path)):
        img = Image.open(image_path_or_array).convert("RGB")
    else:
        img = Image.fromarray(image_path_or_array.astype(np.uint8)).convert("RGB")

    new_hash = imagehash.phash(img, hash_size=HASH_SIZE)
    new_hash_str = str(new_hash)

    # Load the existing index
    index = load_index(index_path)

    if not index:
        # No existing images — always passes
        return {
            "passed": True,
            "phash": new_hash_str,
            "closest_match": None,
            "min_distance": 64,  # Maximum possible distance
            "threshold": threshold,
        }

    # Compare against all existing hashes
    min_distance = 64
    closest_match = None

    for entry in index:
        existing_hash = imagehash.hex_to_hash(entry["phash"])
        distance = new_hash - existing_hash  # Hamming distance
        if distance < min_distance:
            min_distance = distance
            closest_match = entry

    passed = min_distance > threshold

    if passed:
        print(f"[FFL Similarity] PASS — min distance: {min_distance} (threshold: {threshold})")
    else:
        print(f"[FFL Similarity] REJECT — too similar to '{closest_match['palette_name']}' "
              f"({closest_match['directive_id']}) — distance: {min_distance} (threshold: {threshold})")
        print(f"[FFL Similarity] Closest match path: {closest_match['path']}")

    return {
        "passed": passed,
        "phash": new_hash_str,
        "closest_match": closest_match,
        "min_distance": min_distance,
        "threshold": threshold,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 1 INTEGRATION: FULL QC GATE
# This is the single function Agent 1 must call after generating each image.
# It combines D-value QC (from ffl_fractal_generator) with similarity rejection.
# ─────────────────────────────────────────────────────────────────────────────

def full_qc_gate(image_path, palette_name, directive_id, d_value, fractal_type,
                 d_min_julia=1.3, d_min_mandelbrot=1.2, d_max=1.5,
                 similarity_threshold=None, index_path=None):
    """
    Combined QC gate for Agent 1. Checks:
      1. D-value is within the acceptable range for the fractal type
      2. The image is not too similar to any previously accepted image

    Parameters:
      image_path:     str — path to the generated PNG
      palette_name:   str — e.g. 'SAGE-LATTICE'
      directive_id:   str — e.g. 'VBS-001'
      d_value:        float — measured D-value from ffl_fractal_generator.measure_d_value()
      fractal_type:   str — 'mandelbrot', 'julia', 'burning_ship', or 'newton'
      d_min_julia:    float — minimum D-value for Julia/Newton (default 1.3)
      d_min_mandelbrot: float — minimum D-value for Mandelbrot/Burning Ship (default 1.2)
      d_max:          float — maximum D-value for all types (default 1.5)
      similarity_threshold: int — Hamming distance threshold (default: REJECT_THRESHOLD)
      index_path:     str — path to the similarity index JSON

    Returns:
      dict with keys:
        'passed':           bool — True if both QC checks pass
        'd_value_passed':   bool
        'similarity_passed': bool
        'rejection_reason': str or None
        'similarity_result': dict — full result from check_similarity()
    """
    result = {
        "passed": False,
        "d_value_passed": False,
        "similarity_passed": False,
        "rejection_reason": None,
        "similarity_result": None,
    }

    # ── Check 1: D-value ────────────────────────────────────────────────────
    if fractal_type in ("mandelbrot", "burning_ship"):
        d_min = d_min_mandelbrot
    else:
        d_min = d_min_julia

    if not (d_min <= d_value <= d_max):
        result["rejection_reason"] = (
            f"D-value {d_value:.3f} out of range [{d_min:.1f}, {d_max:.1f}] "
            f"for fractal type '{fractal_type}'"
        )
        print(f"[FFL QC Gate] FAIL — {result['rejection_reason']}")
        return result

    result["d_value_passed"] = True

    # ── Check 2: Similarity ─────────────────────────────────────────────────
    sim_result = check_similarity(image_path, similarity_threshold, index_path)
    result["similarity_result"] = sim_result

    if not sim_result["passed"]:
        closest = sim_result["closest_match"]
        result["rejection_reason"] = (
            f"Image too similar to existing asset '{closest['palette_name']}' "
            f"({closest['directive_id']}) — Hamming distance: {sim_result['min_distance']} "
            f"(threshold: {sim_result['threshold']})"
        )
        print(f"[FFL QC Gate] FAIL — {result['rejection_reason']}")
        return result

    result["similarity_passed"] = True
    result["passed"] = True

    # ── Both checks passed: add to index ────────────────────────────────────
    add_to_index(palette_name, directive_id, image_path, index_path)
    print(f"[FFL QC Gate] PASS — {palette_name} ({directive_id}) accepted and indexed.")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST
# Run this file directly to test the similarity checker with the current batch.
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import glob

    TEST_INDEX = "/tmp/ffl_similarity_test_index.json"
    TEST_IMAGES_DIR = "/tmp/ffl_test_batch_v3"

    print("=" * 60)
    print("FFL Similarity Checker — Self-Test")
    print("=" * 60)

    images = sorted(glob.glob(f"{TEST_IMAGES_DIR}/TEST_*.png"))
    if not images:
        print(f"No test images found in {TEST_IMAGES_DIR}")
        exit(1)

    print(f"\nTesting {len(images)} images from {TEST_IMAGES_DIR}\n")

    # Clear test index
    if os.path.exists(TEST_INDEX):
        os.remove(TEST_INDEX)

    for img_path in images:
        palette = os.path.basename(img_path).replace("TEST_", "").replace(".png", "")
        result = check_similarity(img_path, index_path=TEST_INDEX)

        if result["passed"]:
            add_to_index(palette, "TEST-001", img_path, TEST_INDEX)
            print(f"  {palette:30s} ACCEPTED (min_dist={result['min_distance']})\n")
        else:
            closest = result["closest_match"]
            print(f"  {palette:30s} REJECTED — too similar to {closest['palette_name']} "
                  f"(dist={result['min_distance']})\n")

    index = load_index(TEST_INDEX)
    print(f"\nFinal index size: {len(index)} accepted images")
    print("\nSelf-test complete.")
