#!/usr/bin/env python3
"""QR Enhancement Utilities — grid analysis, normalization, slot detection, scan verification.

All functions in this module are designed to degrade gracefully when Pillow
or other optional dependencies are unavailable.  They return structured dicts
with a ``status`` field that is always present.

This module is imported by *material_skill.py* and may also be called
standalone for debugging::

    python3 qr_enhance.py analyze-grid  --qr assets/qr_code.png
    python3 qr_enhance.py normalize     --qr assets/qr_code.png --out qr_normalized.png
    python3 qr_enhance.py detect-slot   --poster material_01_ai.png --min-size 200
    python3 qr_enhance.py verify        --source assets/qr_code.png --final material_01.png --qr-rect 660,895,330,330
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# QR version / module grid constants
# ---------------------------------------------------------------------------

# QR version N → (N-1)*4+21 modules per side.  Versions 1-40.
_QR_MODULES = {v: (v - 1) * 4 + 21 for v in range(1, 41)}
_MODULE_COUNTS = sorted(_QR_MODULES.values())  # [21, 25, 29, …, 177]

_QUIET_ZONE_MODULES = 4  # spec mandates at least 4 modules


def _try_pillow():
    """Return (Image, ImageFilter) or (None, None)."""
    try:
        from PIL import Image, ImageFilter  # type: ignore
        return Image, ImageFilter
    except ImportError:
        return None, None


# ---------------------------------------------------------------------------
# 1. analyze_qr_grid — detect QR version / module count / snap target
# ---------------------------------------------------------------------------

def analyze_qr_grid(qr_path: Path) -> dict[str, Any]:
    """Detect QR version, module count, and compute grid-snap target size.

    Strategy
    --------
    1. Open QR image, convert to grayscale, binarize at threshold 128.
    2. Scan top row of the image for the first run-length of dark pixels
       that looks like a finder pattern timing bar.  QR finder patterns have
       a fixed 7-module dark-light-dark-light-dark pattern; the width of the
       first dark run ≈ 1 module width in pixels.
    3. From module pixel width, estimate total modules = image_width / module_px.
    4. Snap to the nearest valid QR module count (21, 25, 29 … 177).

    Returns a dict with keys:
        status:  "ok" | "fallback"
        module_count:  int (e.g. 33)
        version:  int (e.g. 4) or None
        module_px:  float — estimated pixels per module in the *source* image
        quiet_zone_modules:  int (always 4)
        confidence:  float 0-1
        reason:  str
    """
    Image, _ = _try_pillow()
    if Image is None:
        return {
            "status": "fallback",
            "module_count": None,
            "version": None,
            "module_px": None,
            "quiet_zone_modules": _QUIET_ZONE_MODULES,
            "confidence": 0.0,
            "reason": "Pillow not available",
        }
    try:
        img = Image.open(qr_path).convert("L")
    except Exception as exc:
        return {
            "status": "fallback",
            "module_count": None,
            "version": None,
            "module_px": None,
            "quiet_zone_modules": _QUIET_ZONE_MODULES,
            "confidence": 0.0,
            "reason": f"Cannot open QR image: {exc}",
        }

    w, h = img.size
    pixels = list(img.getdata())

    # --- Find the QR body bounding box (non-white region) ---
    # Binarize: <128 = dark (QR module), >=128 = light (background/quiet zone)
    threshold = 128
    dark_rows_top, dark_rows_bottom = h, 0
    dark_cols_left, dark_cols_right = w, 0
    for y in range(h):
        for x in range(w):
            if pixels[y * w + x] < threshold:
                dark_rows_top = min(dark_rows_top, y)
                dark_rows_bottom = max(dark_rows_bottom, y)
                dark_cols_left = min(dark_cols_left, x)
                dark_cols_right = max(dark_cols_right, x)

    if dark_rows_top >= dark_rows_bottom or dark_cols_left >= dark_cols_right:
        return {
            "status": "fallback",
            "module_count": None,
            "version": None,
            "module_px": None,
            "quiet_zone_modules": _QUIET_ZONE_MODULES,
            "confidence": 0.0,
            "reason": "No dark pixels found in QR image",
        }

    body_w = dark_cols_right - dark_cols_left + 1
    body_h = dark_rows_bottom - dark_rows_top + 1

    # --- Scan the top edge of QR body for finder pattern ---
    # The top-left finder pattern starts with 7 dark modules.
    # Scan the first row of the body to find the first dark run length.
    scan_y = dark_rows_top
    runs: list[tuple[bool, int]] = []  # (is_dark, length)
    current_dark = pixels[scan_y * w + dark_cols_left] < threshold
    run_len = 0
    for x in range(dark_cols_left, dark_cols_right + 1):
        is_dark = pixels[scan_y * w + x] < threshold
        if is_dark == current_dark:
            run_len += 1
        else:
            runs.append((current_dark, run_len))
            current_dark = is_dark
            run_len = 1
    runs.append((current_dark, run_len))

    # Finder pattern top row: 7 dark modules (the entire top row of the
    # 7×7 finder). The first dark run should be ~7 modules wide.
    first_dark_run = None
    for is_dark, length in runs:
        if is_dark and length > 3:  # at least a few pixels
            first_dark_run = length
            break

    if first_dark_run is None:
        return {
            "status": "fallback",
            "module_count": None,
            "version": None,
            "module_px": None,
            "quiet_zone_modules": _QUIET_ZONE_MODULES,
            "confidence": 0.0,
            "reason": "Cannot detect finder pattern in QR image",
        }

    # first_dark_run ≈ 7 × module_px  →  module_px ≈ first_dark_run / 7
    module_px_est = first_dark_run / 7.0

    # Estimate total modules from body width (should be same as height for square QR)
    body_dim = (body_w + body_h) / 2.0  # average to reduce noise
    estimated_modules = body_dim / module_px_est

    # Snap to nearest valid module count
    best_count = min(_MODULE_COUNTS, key=lambda c: abs(c - estimated_modules))
    version = None
    for v, c in _QR_MODULES.items():
        if c == best_count:
            version = v
            break

    # Confidence: how close is the estimate to the snapped count
    deviation = abs(estimated_modules - best_count) / best_count
    confidence = max(0.0, min(1.0, 1.0 - deviation * 5))

    # Also verify by cross-checking: 7 modules (finder) should be ~first_dark_run px
    # and total modules × module_px should be ~body_dim
    recalc_module_px = body_dim / best_count

    return {
        "status": "ok" if confidence >= 0.4 else "fallback",
        "module_count": best_count,
        "version": version,
        "module_px": round(recalc_module_px, 2),
        "quiet_zone_modules": _QUIET_ZONE_MODULES,
        "confidence": round(confidence, 3),
        "reason": f"Detected V{version} ({best_count} modules), "
                  f"est {estimated_modules:.1f} modules, "
                  f"module_px={recalc_module_px:.2f}, "
                  f"finder_run={first_dark_run}px",
    }


def snap_to_grid(target_size: int, module_count: int | None, quiet_zone_modules: int = 4) -> dict[str, Any]:
    """Snap a target pixel size to the nearest module-grid-aligned size.

    If *module_count* is None or 0, returns the original size unchanged.

    The snapped size includes quiet zone on all sides:
        total_modules = module_count + 2 * quiet_zone_modules
        pixels_per_module = round(target_size / total_modules)
        snapped = total_modules * pixels_per_module

    Returns dict with: snapped_size, pixels_per_module, total_modules,
    qr_body_modules, quiet_zone_modules, original_size.
    """
    if not module_count or module_count < 21:
        return {
            "enabled": False,
            "snapped_size": target_size,
            "pixels_per_module": None,
            "total_modules": None,
            "qr_body_modules": module_count,
            "quiet_zone_modules": quiet_zone_modules,
            "original_size": target_size,
        }

    # For snap calculation, we work with QR body modules only (no quiet zone)
    # because the quiet zone is part of the surrounding poster, not the QR image.
    # The QR image itself contains body + its own quiet zone if any.
    # We snap the QR body size to module grid.
    ppm = target_size / module_count
    ppm_rounded = max(1, round(ppm))
    snapped = module_count * ppm_rounded

    return {
        "enabled": True,
        "snapped_size": snapped,
        "pixels_per_module": ppm_rounded,
        "total_modules": module_count,
        "qr_body_modules": module_count,
        "quiet_zone_modules": quiet_zone_modules,
        "original_size": target_size,
    }


# ---------------------------------------------------------------------------
# 2. normalize_qr — crop decorative borders, keep quiet zone + icon
# ---------------------------------------------------------------------------

def normalize_qr(qr_path: Path, out_path: Path) -> dict[str, Any]:
    """Normalize QR source image: crop outer decorative borders, keep QR body.

    Strategy:
    1. Convert to grayscale, binarize at 128.
    2. Find bounding box of dark pixels (QR body outer edge).
    3. Expand by quiet_zone margin (min 4 modules estimated).
    4. If the crop area is 30%-95% of the original image area, crop it.
       Otherwise, fall back to using the original image unchanged.
    5. Save as PNG to out_path.

    Returns dict with: status, crop_box, original_size, cropped_size,
    area_ratio, reason, path.
    """
    Image, _ = _try_pillow()
    if Image is None:
        return {
            "status": "fallback",
            "crop_box": None,
            "original_size": None,
            "cropped_size": None,
            "area_ratio": None,
            "reason": "Pillow not available",
            "path": str(qr_path),
        }

    try:
        img = Image.open(qr_path)
    except Exception as exc:
        return {
            "status": "fallback",
            "crop_box": None,
            "original_size": None,
            "cropped_size": None,
            "area_ratio": None,
            "reason": f"Cannot open QR image: {exc}",
            "path": str(qr_path),
        }

    orig_w, orig_h = img.size
    gray = img.convert("L")
    pixels = list(gray.getdata())
    threshold = 128

    # Find dark pixel bounding box
    top, bottom, left, right = orig_h, 0, orig_w, 0
    for y in range(orig_h):
        for x in range(orig_w):
            if pixels[y * orig_w + x] < threshold:
                top = min(top, y)
                bottom = max(bottom, y)
                left = min(left, x)
                right = max(right, x)

    if top >= bottom or left >= right:
        # No dark pixels — probably not a valid QR, use original
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "PNG")
        return {
            "status": "fallback",
            "crop_box": None,
            "original_size": [orig_w, orig_h],
            "cropped_size": [orig_w, orig_h],
            "area_ratio": 1.0,
            "reason": "No dark pixels found; using original image",
            "path": str(out_path),
        }

    body_w = right - left + 1
    body_h = bottom - top + 1

    # Estimate module size from body and add quiet zone padding
    est_modules = max(21, round(((body_w + body_h) / 2.0) / max(1, body_w / 21)))
    # Simpler: just use a percentage-based quiet zone margin
    quiet_margin = max(8, round(min(body_w, body_h) * 0.08))

    crop_left = max(0, left - quiet_margin)
    crop_top = max(0, top - quiet_margin)
    crop_right = min(orig_w, right + 1 + quiet_margin)
    crop_bottom = min(orig_h, bottom + 1 + quiet_margin)

    crop_w = crop_right - crop_left
    crop_h = crop_bottom - crop_top
    area_ratio = (crop_w * crop_h) / max(1, orig_w * orig_h)

    # Only crop if the result is 30%-95% of the original (meaningful crop)
    if 0.30 <= area_ratio <= 0.95:
        cropped = img.crop((crop_left, crop_top, crop_right, crop_bottom))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(out_path, "PNG")
        return {
            "status": "ok",
            "crop_box": [crop_left, crop_top, crop_right, crop_bottom],
            "original_size": [orig_w, orig_h],
            "cropped_size": [crop_w, crop_h],
            "area_ratio": round(area_ratio, 4),
            "reason": f"Cropped outer border: {orig_w}x{orig_h} -> {crop_w}x{crop_h}",
            "path": str(out_path),
        }
    else:
        # No meaningful crop needed — use original
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "PNG")
        return {
            "status": "unchanged",
            "crop_box": None,
            "original_size": [orig_w, orig_h],
            "cropped_size": [orig_w, orig_h],
            "area_ratio": round(area_ratio, 4),
            "reason": f"Crop area ratio {area_ratio:.2%} outside 30%-95% range; using original",
            "path": str(out_path),
        }


# ---------------------------------------------------------------------------
# 3. detect_qr_slot — find AI-generated QR hosting slot in poster
# ---------------------------------------------------------------------------

def detect_qr_slot(
    poster_path: Path,
    min_slot_size: int = 200,
    *,
    high_score_threshold: float = 0.75,
    low_score_threshold: float = 0.50,
    min_area_ratio: float = 0.04,
    max_area_ratio: float = 0.15,
    prefer_framed_slot: bool = False,
) -> dict[str, Any]:
    """Detect a QR hosting slot (bright, low-texture rectangular area) in the poster.

    Strategy:
    1. Convert poster to grayscale.
    2. Binarize at high threshold (200) to find bright regions.
    3. Find connected components via flood-fill on the binary image.
    4. Filter candidates: area >= min_slot_size², aspect ratio near square,
       not in top 15% of poster (headline zone).
    5. For each candidate, compute:
       - mean luminance (want high, >200)
       - Laplacian variance (want low, <15 — indicates flat/clean area)
       - saturation (want low)
    6. Score each candidate, pick the best.
    7. Apply edge-shrink: from each edge inward, find the stable clean boundary.

    Returns dict with:
        status: "detected_slot" | "soft_slot" | "no_slot"
        host_rect: {x, y, w, h}  — outer bounding box of the detected slot
        inner_rect: {x, y, w, h} — edge-shrunk clean interior
        slot_score: float 0-1
        slot_luminance: float
        laplacian_variance: float
        reason: str
    """
    Image, ImageFilter = _try_pillow()
    if Image is None:
        return {"status": "no_slot", "reason": "Pillow not available"}

    try:
        img = Image.open(poster_path).convert("RGB")
    except Exception as exc:
        return {"status": "no_slot", "reason": f"Cannot open poster: {exc}"}

    img_w, img_h = img.size
    if img_w < 100 or img_h < 100:
        return {"status": "no_slot", "reason": "Poster too small for slot detection"}

    # --- Step 1: Build binary mask of bright pixels ---
    gray = img.convert("L")
    gray_pixels = list(gray.getdata())
    # Use 210 to catch warm-white / off-white QR slot interiors (e.g. wooden frame
    # with slightly warm fill) while still excluding cream/beige scene backgrounds.
    bright_threshold = 210

    # Downsample for performance (work at half resolution if large)
    scale = 1
    work_w, work_h = img_w, img_h
    if img_w > 800 or img_h > 800:
        scale = 2
        work_w, work_h = img_w // 2, img_h // 2
        gray = gray.resize((work_w, work_h), Image.NEAREST)
        gray_pixels = list(gray.getdata())

    binary = [1 if p >= bright_threshold else 0 for p in gray_pixels]

    # --- Step 2: Connected component labeling (simple flood-fill) ---
    labels = [0] * len(binary)
    label_id = 0
    component_pixels: dict[int, list[tuple[int, int]]] = {}

    def flood_fill(start_x: int, start_y: int, lid: int) -> None:
        stack = [(start_x, start_y)]
        while stack:
            sx, sy = stack.pop()
            idx = sy * work_w + sx
            if sx < 0 or sx >= work_w or sy < 0 or sy >= work_h:
                continue
            if labels[idx] != 0 or binary[idx] != 1:
                continue
            labels[idx] = lid
            component_pixels.setdefault(lid, []).append((sx, sy))
            # Limit component size to avoid huge floods
            if len(component_pixels[lid]) > work_w * work_h // 2:
                return
            stack.extend([
                (sx + 1, sy), (sx - 1, sy), (sx, sy + 1), (sx, sy - 1),
            ])

    min_area = (min_slot_size // scale) ** 2 // 4  # relaxed min for downsampled
    for y in range(work_h):
        for x in range(work_w):
            idx = y * work_w + x
            if binary[idx] == 1 and labels[idx] == 0:
                label_id += 1
                flood_fill(x, y, label_id)

    # --- Step 3: Evaluate each connected component ---
    candidates: list[dict[str, Any]] = []
    headline_cutoff = work_h * 0.15  # skip top 15%

    for lid, px_list in component_pixels.items():
        if len(px_list) < min_area:
            continue
        xs = [p[0] for p in px_list]
        ys = [p[1] for p in px_list]
        bbox_x = min(xs)
        bbox_y = min(ys)
        bbox_w = max(xs) - bbox_x + 1
        bbox_h = max(ys) - bbox_y + 1

        # Skip components mostly in headline zone
        center_y = bbox_y + bbox_h / 2
        if center_y < headline_cutoff:
            continue

        # 2a. Aspect ratio hard filter: QR slot must be roughly square
        aspect = bbox_w / max(1, bbox_h)
        if aspect < 0.55 or aspect > 1.8:
            continue

        # Size filter: both dimensions should be large enough
        min_dim = min(bbox_w, bbox_h) * scale
        if min_dim < min_slot_size * 0.8:
            continue

        # 2b. Area ratio filter: QR slot should be within the caller's expected
        # footprint.  Artistic and small-format materials can intentionally use
        # more compact QR hosts than the default 4% floor.
        candidate_area_ratio = len(px_list) / max(1, work_w * work_h)
        if candidate_area_ratio < min_area_ratio or candidate_area_ratio > max_area_ratio:
            continue

        # Fill ratio: how much of the bounding box is actually bright
        fill_ratio = len(px_list) / max(1, bbox_w * bbox_h)

        # Mean luminance of the component
        lum_sum = sum(gray_pixels[py * work_w + px] for px, py in px_list)
        mean_lum = lum_sum / max(1, len(px_list))

        # Laplacian variance (texture indicator) — compute on the bounding box
        lap_var = _laplacian_variance_region(gray_pixels, work_w, work_h,
                                             bbox_x, bbox_y, bbox_w, bbox_h)

        # Edge-ring texture check: preserve the old decorative-frame guard, but
        # distinguish regular graphic frames from complex ornamentation.
        edge_band = max(2, min(bbox_w, bbox_h) // 8)
        edge_lap = _edge_ring_laplacian(gray_pixels, work_w, work_h,
                                        bbox_x, bbox_y, bbox_w, bbox_h, edge_band)
        border_metrics = _classify_border_frame(
            gray_pixels, work_w, work_h, bbox_x, bbox_y, bbox_w, bbox_h, mean_lum,
        )
        border_kind = border_metrics["border_kind"]

        # 2c. Border contrast check: detect if candidate blends into background
        outer_band = max(2, min(bbox_w, bbox_h) // 20)
        outer_x = max(0, bbox_x - outer_band)
        outer_y = max(0, bbox_y - outer_band)
        outer_w = min(work_w - outer_x, bbox_w + 2 * outer_band)
        outer_h = min(work_h - outer_y, bbox_h + 2 * outer_band)
        outer_lum = _region_mean_luminance(gray_pixels, work_w, work_h,
                                                outer_x, outer_y, outer_w, outer_h)
        border_contrast = mean_lum - outer_lum
        low_border_contrast = abs(border_contrast) < 30

        # Saturation check: want low saturation in the slot region
        # (skip if too expensive; luminance is a good proxy)

        # Score the candidate
        lum_score = max(0.0, min(1.0, (mean_lum - 180) / 75))  # 180→0, 255→1
        fill_score = max(0.0, min(1.0, fill_ratio))
        texture_penalty = min(1.0, lap_var / 30.0)  # low variance → low penalty
        aspect_score = 1.0 - abs(1.0 - aspect) * 0.3  # prefer square
        frame_bonus = 0.15 if prefer_framed_slot and border_kind == "regular_frame" else 0.0
        frame_penalty = 0.35 if border_kind == "decorative_frame" else 0.0
        contrast_penalty = 0.25 if low_border_contrast else 0.0
        position_score = 0.0
        cy_ratio = center_y / max(1, work_h)
        cx_ratio = (bbox_x + bbox_w / 2) / max(1, work_w)
        # Prefer right-center and right-bottom (40%-80% height, right 60%)
        if 0.40 <= cy_ratio <= 0.80:
            position_score = 0.20
        elif 0.30 <= cy_ratio <= 0.90:
            position_score = 0.10
        # Penalize top area (< 30% height) — conflicts with headline
        if cy_ratio < 0.30:
            position_score = -0.10
        # Bonus for right-side placement
        if cx_ratio > 0.55:
            position_score += 0.05
        # Penalize left-side placement — QR prompt asks for right/center
        if cx_ratio < 0.40:
            position_score -= 0.15

        score = (
            lum_score * 0.35
            + fill_score * 0.20
            + (1.0 - texture_penalty) * 0.25
            + aspect_score * 0.05
            + position_score
            + frame_bonus
            - frame_penalty
            - contrast_penalty
        )
        score = max(0.0, min(1.0, score))

        # Estimate rotation angle of this component via PCA
        rotation_deg = _min_area_rect_angle(px_list)

        candidates.append({
            "host_rect": {
                "x": bbox_x * scale,
                "y": bbox_y * scale,
                "w": bbox_w * scale,
                "h": bbox_h * scale,
            },
            "slot_score": round(score, 4),
            "slot_luminance": round(mean_lum, 1),
            "laplacian_variance": round(lap_var, 2),
            "fill_ratio": round(fill_ratio, 3),
            "aspect_ratio": round(aspect, 3),
            "border_contrast": round(border_contrast, 1),
            "border_kind": border_kind,
            "border_uniformity": border_metrics["border_uniformity"],
            "edge_strength": border_metrics["edge_strength"],
            "edge_laplacian": round(edge_lap, 2),
            "candidate_area_ratio": round(candidate_area_ratio, 4),
            "pixel_count": len(px_list) * (scale ** 2),
            "rotation_deg": rotation_deg,
        })

    if not candidates:
        return {"status": "no_slot", "reason": "No bright rectangular region found"}

    # Sort by score descending
    candidates.sort(key=lambda c: c["slot_score"], reverse=True)
    best = candidates[0]

    # --- Step 4: Edge shrink on the best candidate ---
    hr = best["host_rect"]
    inner = _edge_shrink(gray_pixels if scale == 1 else list(img.convert("L").resize((img_w, img_h), Image.NEAREST).getdata()),
                         img_w, img_h, hr["x"], hr["y"], hr["w"], hr["h"])

    # Re-read full-res luminance for the inner rect for the luminance-guard
    full_gray = list(img.convert("L").getdata())
    inner_lum = _region_mean_luminance(full_gray, img_w, img_h,
                                       inner["x"], inner["y"], inner["w"], inner["h"])

    # Determine fit mode
    score = best["slot_score"]
    if score >= high_score_threshold:
        fit_mode = "detected_slot"
    elif score >= low_score_threshold:
        fit_mode = "soft_slot"
    else:
        fit_mode = "no_slot"

    rotation_deg = best.get("rotation_deg", 0.0)

    return {
        "status": fit_mode,
        "host_rect": hr,
        "inner_rect": inner,
        "slot_score": best["slot_score"],
        "slot_luminance": round(inner_lum, 1),
        "laplacian_variance": best["laplacian_variance"],
        "fill_ratio": best.get("fill_ratio"),
        "border_contrast": best.get("border_contrast"),
        "border_kind": best.get("border_kind", "none"),
        "border_uniformity": best.get("border_uniformity", 0.0),
        "edge_strength": best.get("edge_strength", 0.0),
        "candidate_area_ratio": best.get("candidate_area_ratio"),
        "aspect_ratio": best.get("aspect_ratio"),
        "rotation_deg": rotation_deg,
        "candidates_count": len(candidates),
        "reason": f"Best slot score={score:.3f}, lum={inner_lum:.0f}, "
                  f"lap_var={best['laplacian_variance']:.1f}, "
                  f"border_contrast={best.get('border_contrast', 0):.0f}, "
                  f"rotation={rotation_deg:.1f}°, "
                  f"fit_mode={fit_mode}",
        "poster_width": img_w,
        "poster_height": img_h,
    }


def _min_area_rect_angle(px_list: list[tuple[int, int]]) -> float:
    """Estimate the rotation angle of a connected component using PCA on its pixel coordinates.

    Returns the clockwise rotation angle in degrees in the range [-45, 45].
    0 means the component is axis-aligned.

    Strategy: compute the 2×2 covariance matrix of (x, y) coordinates, then
    find the principal axis via the analytic eigenvector formula.  The angle
    of the principal axis relative to the horizontal gives the tilt.

    This is a pure-Python approximation of cv2.minAreaRect without OpenCV.

    IMPORTANT: For near-square / isotropic regions the two eigenvalues are
    nearly equal, making the principal axis direction numerically unstable.
    In that case we return 0.0 (axis-aligned) rather than a spurious angle.
    The eigenvalue ratio λ_max/λ_min must exceed a threshold (1.5) before we
    trust the PCA angle.  This prevents false 45° rotations on square slots.
    """
    import math
    n = len(px_list)
    if n < 10:
        return 0.0

    # Compute centroid
    mx = sum(p[0] for p in px_list) / n
    my = sum(p[1] for p in px_list) / n

    # Covariance matrix elements
    cxx = sum((p[0] - mx) ** 2 for p in px_list) / n
    cyy = sum((p[1] - my) ** 2 for p in px_list) / n
    cxy = sum((p[0] - mx) * (p[1] - my) for p in px_list) / n

    # Eigenvalues of 2×2 symmetric matrix [[cxx, cxy],[cxy, cyy]]
    # λ = ((cxx+cyy) ± sqrt((cxx-cyy)²+4·cxy²)) / 2
    trace = cxx + cyy
    disc = math.sqrt(max(0.0, (cxx - cyy) ** 2 + 4 * cxy ** 2))
    lam_max = (trace + disc) / 2.0
    lam_min = (trace - disc) / 2.0

    # Isotropy check: if the two eigenvalues are close, the shape is near-circular
    # or near-square — PCA axis direction is unreliable, return 0.
    # Threshold 2.5: the component must be clearly elongated (not square/round)
    # before we trust the PCA angle.  Square QR slots always fail this check.
    if lam_min <= 0 or lam_max / max(lam_min, 1e-9) < 2.5:
        return 0.0

    # Analytic eigenvector of 2×2 symmetric matrix [[cxx, cxy],[cxy, cyy]]
    # Principal axis angle: atan2(2*cxy, cxx - cyy) / 2
    if abs(cxx - cyy) < 1e-9 and abs(cxy) < 1e-9:
        return 0.0

    angle_rad = 0.5 * math.atan2(2.0 * cxy, cxx - cyy)
    angle_deg = math.degrees(angle_rad)

    # Clamp to [-45, 45] — beyond that the slot is too skewed to be a QR host
    angle_deg = max(-45.0, min(45.0, angle_deg))
    return round(angle_deg, 1)


def _laplacian_variance_region(
    pixels: list[int], img_w: int, img_h: int,
    rx: int, ry: int, rw: int, rh: int,
) -> float:
    """Compute Laplacian variance for a grayscale region (texture indicator).

    Low variance → flat/clean area; high variance → textured/busy area.
    Uses a simple 3×3 Laplacian kernel: [0,1,0; 1,-4,1; 0,1,0].
    """
    total = 0.0
    values: list[float] = []
    step = max(1, min(rw, rh) // 40)  # sample for speed
    for y in range(ry + 1, min(ry + rh - 1, img_h - 1), step):
        for x in range(rx + 1, min(rx + rw - 1, img_w - 1), step):
            center = pixels[y * img_w + x]
            lap = (
                pixels[(y - 1) * img_w + x]
                + pixels[(y + 1) * img_w + x]
                + pixels[y * img_w + (x - 1)]
                + pixels[y * img_w + (x + 1)]
                - 4 * center
            )
            values.append(lap)
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance ** 0.5  # return std dev for more intuitive thresholding


def _edge_ring_laplacian(
    pixels: list[int], img_w: int, img_h: int,
    rx: int, ry: int, rw: int, rh: int,
    band: int,
) -> float:
    """Compute Laplacian variance only on the border ring of a rectangle.

    A decorative frame has high texture on its edges but clean interior.
    A genuine QR slot has clean edges (just white meeting the poster background).
    """
    values: list[float] = []
    step = max(1, band // 2)
    for y in range(ry + 1, min(ry + rh - 1, img_h - 1), step):
        for x in range(rx + 1, min(rx + rw - 1, img_w - 1), step):
            in_ring = (
                y < ry + band or y >= ry + rh - band
                or x < rx + band or x >= rx + rw - band
            )
            if not in_ring:
                continue
            center = pixels[y * img_w + x]
            lap = (
                pixels[(y - 1) * img_w + x]
                + pixels[(y + 1) * img_w + x]
                + pixels[y * img_w + (x - 1)]
                + pixels[y * img_w + (x + 1)]
                - 4 * center
            )
            values.append(lap)
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance ** 0.5


def _classify_border_frame(
    pixels: list[int], img_w: int, img_h: int,
    rx: int, ry: int, rw: int, rh: int,
    inner_lum: float,
) -> dict[str, Any]:
    """Classify the immediate outside ring around a bright slot.

    Regular QR hosts have a consistent contrast band around the white interior.
    Decorative motifs tend to be strong on only some sides or vary heavily along
    a side.  The metric intentionally samples outside the bright component, so a
    dark/magenta frame surrounding a white slot is detected even when it is not
    part of the thresholded white connected component.
    """

    def stats(values: list[float]) -> tuple[float, float]:
        if not values:
            return 0.0, 0.0
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        return mean, variance ** 0.5

    band = max(2, min(rw, rh) // 18)
    step = max(1, min(rw, rh) // 64)
    side_values: dict[str, list[float]] = {"top": [], "bottom": [], "left": [], "right": []}

    for y in range(max(0, ry - band), ry, step):
        for x in range(rx, min(rx + rw, img_w), step):
            side_values["top"].append(abs(inner_lum - pixels[y * img_w + x]))
    for y in range(ry + rh, min(ry + rh + band, img_h), step):
        for x in range(rx, min(rx + rw, img_w), step):
            side_values["bottom"].append(abs(inner_lum - pixels[y * img_w + x]))
    for x in range(max(0, rx - band), rx, step):
        for y in range(ry, min(ry + rh, img_h), step):
            side_values["left"].append(abs(inner_lum - pixels[y * img_w + x]))
    for x in range(rx + rw, min(rx + rw + band, img_w), step):
        for y in range(ry, min(ry + rh, img_h), step):
            side_values["right"].append(abs(inner_lum - pixels[y * img_w + x]))

    side_means: list[float] = []
    side_uniformities: list[float] = []
    for values in side_values.values():
        mean, stdev = stats(values)
        side_means.append(mean)
        side_uniformities.append(1.0 - min(1.0, stdev / max(1.0, mean)))

    edge_strength = sum(side_means) / max(1, len(side_means))
    max_side = max(side_means) if side_means else 0.0
    min_side = min(side_means) if side_means else 0.0
    side_balance = min_side / max(1.0, max_side)
    along_uniformity = sum(side_uniformities) / max(1, len(side_uniformities))
    border_uniformity = max(0.0, min(1.0, side_balance * 0.55 + along_uniformity * 0.45))

    if edge_strength < 30.0:
        border_kind = "none"
    elif side_balance >= 0.70 and border_uniformity >= 0.78:
        border_kind = "regular_frame"
    else:
        border_kind = "decorative_frame"

    return {
        "border_kind": border_kind,
        "border_uniformity": round(border_uniformity, 3),
        "edge_strength": round(edge_strength, 1),
    }


def _edge_shrink(
    pixels: list[int], img_w: int, img_h: int,
    rx: int, ry: int, rw: int, rh: int,
    lum_threshold: int = 220,
    min_clean_ratio: float = 0.80,
) -> dict[str, Any]:
    """Shrink a rectangle inward until each edge row/col is mostly clean (bright).

    Returns the shrunk inner rectangle as {x, y, w, h}.
    """
    max_shrink = min(rw, rh) // 4  # don't shrink more than 25% from each side

    # Shrink from top
    top_shrink = 0
    for dy in range(max_shrink):
        row_y = ry + dy
        if row_y >= img_h:
            break
        bright = sum(1 for x in range(rx, min(rx + rw, img_w))
                     if pixels[row_y * img_w + x] >= lum_threshold)
        if bright / max(1, rw) >= min_clean_ratio:
            break
        top_shrink = dy + 1

    # Shrink from bottom
    bottom_shrink = 0
    for dy in range(max_shrink):
        row_y = ry + rh - 1 - dy
        if row_y < 0:
            break
        bright = sum(1 for x in range(rx, min(rx + rw, img_w))
                     if pixels[row_y * img_w + x] >= lum_threshold)
        if bright / max(1, rw) >= min_clean_ratio:
            break
        bottom_shrink = dy + 1

    # Shrink from left
    left_shrink = 0
    for dx in range(max_shrink):
        col_x = rx + dx
        if col_x >= img_w:
            break
        bright = sum(1 for y in range(ry, min(ry + rh, img_h))
                     if pixels[y * img_w + col_x] >= lum_threshold)
        if bright / max(1, rh) >= min_clean_ratio:
            break
        left_shrink = dx + 1

    # Shrink from right
    right_shrink = 0
    for dx in range(max_shrink):
        col_x = rx + rw - 1 - dx
        if col_x < 0:
            break
        bright = sum(1 for y in range(ry, min(ry + rh, img_h))
                     if pixels[y * img_w + col_x] >= lum_threshold)
        if bright / max(1, rh) >= min_clean_ratio:
            break
        right_shrink = dx + 1

    new_x = rx + left_shrink
    new_y = ry + top_shrink
    new_w = max(1, rw - left_shrink - right_shrink)
    new_h = max(1, rh - top_shrink - bottom_shrink)

    # Force square: use shorter dimension, centered in the longer axis.
    # QR codes are square — a non-square inner rect means decorative elements
    # (labels, ribbons) are still included on one side.
    if new_w != new_h:
        side = min(new_w, new_h)
        new_x += (new_w - side) // 2
        new_y += (new_h - side) // 2
        new_w = side
        new_h = side

    return {"x": new_x, "y": new_y, "w": new_w, "h": new_h}


def _region_mean_luminance(
    pixels: list[int], img_w: int, img_h: int,
    rx: int, ry: int, rw: int, rh: int,
) -> float:
    """Mean luminance of a rectangular region."""
    total = 0.0
    count = 0
    step = max(1, min(rw, rh) // 50)
    for y in range(ry, min(ry + rh, img_h), step):
        for x in range(rx, min(rx + rw, img_w), step):
            total += pixels[y * img_w + x]
            count += 1
    return total / max(1, count)


# ---------------------------------------------------------------------------
# 4. compute_qr_rect — calculate final QR paste rectangle within slot
# ---------------------------------------------------------------------------

def compute_qr_rect(
    inner_rect: dict[str, Any],
    qr_size_px: int,
    grid_snap: dict[str, Any] | None = None,
    fill_ratio: float = 0.88,
) -> dict[str, Any]:
    """Compute the final QR paste rectangle centered in the inner_rect.

    If *grid_snap* is provided and enabled, the QR size is snapped to module grid.
    The QR is centered within inner_rect.

    Uses inner_width (not inner_short) as the reference dimension so QR fills
    the visible width of the slot.  For non-square slots the vertical centering
    absorbs the extra height naturally.
    """
    ix, iy, iw, ih = inner_rect["x"], inner_rect["y"], inner_rect["w"], inner_rect["h"]
    # Use width as reference — QR should fill the visible horizontal span.
    # For truly square slots iw ≈ ih so this is equivalent to the old logic.
    ref_dim = iw

    # Adaptive fill ratio based on how the requested qr_size compares to ref_dim
    size_ratio = qr_size_px / max(1, ref_dim)
    if size_ratio < 0.5:
        effective_fill = 0.78
    elif size_ratio > 0.9:
        effective_fill = 0.93
    else:
        effective_fill = fill_ratio

    # Target QR size within inner rect — also cap to inner height
    target = min(round(ref_dim * effective_fill), ih)

    # Apply grid snap if available
    if grid_snap and grid_snap.get("enabled"):
        mc = grid_snap.get("qr_body_modules") or grid_snap.get("total_modules")
        if mc:
            snapped = snap_to_grid(target, mc)
            target = snapped["snapped_size"]

    # Center within inner_rect
    # If inner rect is taller than wide (likely has a text label below the QR area),
    # center QR in the top square portion to avoid overlapping with the label.
    effective_h = ih
    if ih > iw * 1.15:
        effective_h = iw  # use only the square top portion
    qr_x = ix + (iw - target) // 2
    qr_y = iy + (effective_h - target) // 2

    return {
        "x": qr_x,
        "y": qr_y,
        "w": target,
        "h": target,
    }


# ---------------------------------------------------------------------------
# 5. verify_qr_decode — optional scan verification
# ---------------------------------------------------------------------------

def verify_qr_decode(
    source_qr_path: Path,
    final_image_path: Path,
    qr_rect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attempt to decode QR from both source and final image, compare payloads.

    Tries decoders in order: zbarimg CLI, pyzbar, cv2.QRCodeDetector.
    Returns dict with: status, decoder, source_payload, final_payload,
    payload_match, reason.
    """
    result: dict[str, Any] = {
        "status": "unavailable",
        "decoder": None,
        "source_payload": None,
        "final_payload": None,
        "payload_match": None,
        "reason": "No QR decoder available",
    }

    # Try zbarimg CLI first
    zbarimg = shutil.which("zbarimg")
    if zbarimg:
        try:
            source_payload = _decode_with_zbarimg(zbarimg, source_qr_path)
            final_payload = _decode_with_zbarimg(zbarimg, final_image_path, qr_rect)
            result.update({
                "status": "ok" if source_payload and final_payload else "partial",
                "decoder": "zbarimg",
                "source_payload": source_payload,
                "final_payload": final_payload,
                "payload_match": source_payload == final_payload if source_payload and final_payload else None,
                "reason": "Decoded with zbarimg CLI",
            })
            return result
        except Exception as exc:
            result["reason"] = f"zbarimg failed: {exc}"

    # Try pyzbar
    try:
        from pyzbar import pyzbar as pyzbar_mod  # type: ignore
        Image, _ = _try_pillow()
        if Image:
            source_payload = _decode_with_pyzbar(pyzbar_mod, Image, source_qr_path)
            final_payload = _decode_with_pyzbar(pyzbar_mod, Image, final_image_path, qr_rect)
            result.update({
                "status": "ok" if source_payload and final_payload else "partial",
                "decoder": "pyzbar",
                "source_payload": source_payload,
                "final_payload": final_payload,
                "payload_match": source_payload == final_payload if source_payload and final_payload else None,
                "reason": "Decoded with pyzbar",
            })
            return result
    except ImportError:
        pass

    # Try cv2
    try:
        import cv2  # type: ignore
        source_payload = _decode_with_cv2(cv2, source_qr_path)
        final_payload = _decode_with_cv2(cv2, final_image_path, qr_rect)
        result.update({
            "status": "ok" if source_payload and final_payload else "partial",
            "decoder": "cv2.QRCodeDetector",
            "source_payload": source_payload,
            "final_payload": final_payload,
            "payload_match": source_payload == final_payload if source_payload and final_payload else None,
            "reason": "Decoded with OpenCV QRCodeDetector",
        })
        return result
    except ImportError:
        pass

    return result


def _decode_with_zbarimg(
    zbarimg_path: str, image_path: Path, qr_rect: dict[str, Any] | None = None,
) -> str | None:
    """Decode QR using zbarimg CLI. Returns decoded string or None."""
    # If qr_rect is provided, we'd need to crop first — for simplicity,
    # decode the full image (zbarimg will find the QR)
    try:
        proc = subprocess.run(
            [zbarimg_path, "--quiet", "--raw", str(image_path)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().split("\n")[0]
    except Exception:
        pass
    return None


def _decode_with_pyzbar(
    pyzbar_mod: Any, Image: Any, image_path: Path, qr_rect: dict[str, Any] | None = None,
) -> str | None:
    """Decode QR using pyzbar. Returns decoded string or None."""
    try:
        img = Image.open(image_path)
        if qr_rect:
            x, y, w, h = qr_rect["x"], qr_rect["y"], qr_rect["w"], qr_rect["h"]
            img = img.crop((x, y, x + w, y + h))
        decoded = pyzbar_mod.decode(img)
        if decoded:
            return decoded[0].data.decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def _decode_with_cv2(
    cv2: Any, image_path: Path, qr_rect: dict[str, Any] | None = None,
) -> str | None:
    """Decode QR using OpenCV QRCodeDetector. Returns decoded string or None."""
    try:
        img = cv2.imread(str(image_path))
        if img is None:
            return None
        if qr_rect:
            x, y, w, h = qr_rect["x"], qr_rect["y"], qr_rect["w"], qr_rect["h"]
            img = img[y:y + h, x:x + w]
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(img)
        return data if data else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# CLI for standalone debugging
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QR Enhancement Utilities")
    sub = parser.add_subparsers(dest="command")

    # analyze-grid
    p_grid = sub.add_parser("analyze-grid", help="Detect QR version and module grid")
    p_grid.add_argument("--qr", required=True, help="Path to QR image")

    # normalize
    p_norm = sub.add_parser("normalize", help="Normalize QR source image")
    p_norm.add_argument("--qr", required=True, help="Path to QR image")
    p_norm.add_argument("--out", required=True, help="Output path for normalized QR")

    # detect-slot
    p_slot = sub.add_parser("detect-slot", help="Detect QR slot in poster")
    p_slot.add_argument("--poster", required=True, help="Path to poster image")
    p_slot.add_argument("--min-size", type=int, default=200, help="Minimum slot size in px")
    p_slot.add_argument("--min-area-ratio", type=float, default=0.04, help="Minimum bright slot area ratio")
    p_slot.add_argument("--max-area-ratio", type=float, default=0.15, help="Maximum bright slot area ratio")
    p_slot.add_argument("--prefer-framed-slot", action="store_true", help="Reward regular framed QR slots")

    # verify
    p_verify = sub.add_parser("verify", help="Verify QR decode in final image")
    p_verify.add_argument("--source", required=True, help="Path to source QR image")
    p_verify.add_argument("--final", required=True, help="Path to final composited image")
    p_verify.add_argument("--qr-rect", help="x,y,w,h of QR region in final image")

    args = parser.parse_args(argv)

    if args.command == "analyze-grid":
        result = analyze_qr_grid(Path(args.qr))
    elif args.command == "normalize":
        result = normalize_qr(Path(args.qr), Path(args.out))
    elif args.command == "detect-slot":
        result = detect_qr_slot(
            Path(args.poster),
            min_slot_size=args.min_size,
            min_area_ratio=args.min_area_ratio,
            max_area_ratio=args.max_area_ratio,
            prefer_framed_slot=args.prefer_framed_slot,
        )
    elif args.command == "verify":
        qr_rect = None
        if args.qr_rect:
            parts = [int(x) for x in args.qr_rect.split(",")]
            qr_rect = {"x": parts[0], "y": parts[1], "w": parts[2], "h": parts[3]}
        result = verify_qr_decode(Path(args.source), Path(args.final), qr_rect)
    else:
        parser.print_help()
        return 0

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
