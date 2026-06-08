#!/usr/bin/env python3
"""QR Post-Composition Script — 后合成二维码到最终海报图.

Takes an AI poster PNG (generated without QR modules) and composites the
original QR code image onto it using placement coordinates from
qr_placement.json.  The QR image is NEVER recolored or redrawn — it is only
proportionally scaled with NEAREST interpolation and pasted to preserve
scanability with pixel-perfect module edges.

Three fit modes
---------------
- **detected_slot**: High-confidence AI slot detected.  QR is pasted directly
  into the slot interior — no extra white card, no shadow.  If the inner region
  luminance is below 200, a minimal white underlay is added for contrast.
- **soft_slot**: Medium-confidence slot.  A semi-transparent white underlay
  (alpha ~128) is placed behind QR for safety, but no full card tray.
- **fallback_card**: No reliable slot found.  A rounded white card tray with
  optional shadow is composited (original behavior).

Grid-snap
---------
When ``grid_snap`` info is present in the placement JSON, the QR is resized to
an exact module-grid-aligned dimension so every QR module is the same integer
number of pixels — eliminating uneven module sizes from non-integer scaling.

Typical pipeline
----------------
material_skill.py produces:
  - material_01_ai.png (AI poster artwork, everything except real QR modules)
  - qr_placement.json  (slot detection + program-scored placement)
  - assets/qr_code.png (original user-supplied QR, byte-identical copy)

Then this script composites:
  material_01_ai.png + qr_code.png + qr_placement.json  →  material_01.png

Usage
-----
python3 qr_composite.py \\
    --poster materials/material_01_ai.png \\
    --qr     assets/qr_code.png \\
    --placement variants/variant_01/qr_placement.json \\
    --out    materials/material_01.png

Optional flags
--------------
--fit-mode MODE     One of: detected_slot, soft_slot, fallback_card, auto (default: auto)
--padding INT       Padding around QR inside card in fallback_card mode (default: auto)
--border-radius INT Rounded corner radius for the card tray (default: 12)
--shadow            Add soft drop shadow behind card in fallback_card mode (default: on)
--no-shadow         Disable drop shadow
--qr-fraction FLOAT Multiplier for requested QR body size (default: 1.0)
--card-color HEX    Card tray background color (default: #ffffff)
--dry-run           Print resolved parameters and exit without writing

Exit codes
----------
0  Success
1  Input file not found or unreadable
2  Pillow not installed
3  Invalid placement JSON
4  Composition error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Pillow import guard
# ---------------------------------------------------------------------------

def _require_pillow() -> Any:
    try:
        from PIL import Image, ImageDraw, ImageFilter  # type: ignore
        return Image, ImageDraw, ImageFilter
    except ImportError:
        print(
            "ERROR: Pillow is not installed.\n"
            "Install with:  pip install Pillow   or   uv pip install Pillow",
            file=sys.stderr,
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_placement(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"ERROR: placement file not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON in placement file: {exc}", file=sys.stderr)
        sys.exit(3)
    # New format uses qr_rect; legacy uses x_px/y_px/size_px
    has_new = "qr_rect" in data
    has_legacy = "x_px" in data and "y_px" in data and "size_px" in data
    if not has_new and not has_legacy:
        print("ERROR: placement JSON missing required fields (need qr_rect or x_px/y_px/size_px)", file=sys.stderr)
        sys.exit(3)
    return data


def hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """Parse #RRGGBB → (R, G, B, A).  Falls back to white on error."""
    try:
        h = hex_color.lstrip("#")
        if len(h) == 6:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha
    except (ValueError, AttributeError):
        pass
    return 255, 255, 255, alpha


def make_rounded_mask(width: int, height: int, radius: int) -> Any:
    """L-mode image: white rounded rect on black — usable as paste mask."""
    from PIL import Image, ImageDraw  # type: ignore
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=255)
    return mask


def add_shadow(
    base: Any,
    card_x: int,
    card_y: int,
    card_w: int,
    card_h: int,
    blur_radius: int = 14,
    shadow_alpha: int = 70,
    offset_x: int = 0,
    offset_y: int = 5,
) -> Any:
    """Composite a soft drop shadow behind the card region."""
    from PIL import Image, ImageFilter  # type: ignore
    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_rect = Image.new("RGBA", (card_w, card_h), (0, 0, 0, shadow_alpha))
    shadow_layer.paste(shadow_rect, (card_x + offset_x, card_y + offset_y))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur_radius))
    return Image.alpha_composite(base, shadow_layer)


def _region_mean_luminance(img_rgba: Any, x: int, y: int, w: int, h: int) -> float:
    """Compute mean luminance of a region in an RGBA image."""
    from PIL import Image  # type: ignore
    region = img_rgba.crop((x, y, x + w, y + h)).convert("L")
    pixels = list(region.getdata())
    return sum(pixels) / max(1, len(pixels))


# ---------------------------------------------------------------------------
# Resolve placement: support both new (qr_rect) and legacy (x_px/y_px/size_px)
# ---------------------------------------------------------------------------

def _resolve_placement(
    placement: dict[str, Any],
    poster_w: int,
    poster_h: int,
    qr_fraction: float,
) -> dict[str, Any]:
    """Normalize placement dict into a standard internal format.

    Returns dict with: qr_x, qr_y, qr_size, fit_mode, grid_snap, slot_luminance.
    """
    fit_mode = placement.get("fit_mode", "fallback_card")

    # New format: qr_rect takes priority
    qr_rect = placement.get("qr_rect")
    if qr_rect:
        qr_x = int(qr_rect["x"])
        qr_y = int(qr_rect["y"])
        qr_size = int(qr_rect["w"])  # assume square
    else:
        # Legacy format
        qr_x = int(placement["x_px"])
        qr_y = int(placement["y_px"])
        qr_size = int(placement["size_px"])
        canvas_w = int(placement.get("canvas_width", poster_w))
        canvas_h = int(placement.get("canvas_height", poster_h))
        # Scale if canvas differs from poster
        scale_x = poster_w / max(1, canvas_w)
        scale_y = poster_h / max(1, canvas_h)
        qr_x = round(qr_x * scale_x)
        qr_y = round(qr_y * scale_y)
        qr_size = round(qr_size * min(scale_x, scale_y))

    qr_size = max(80, qr_size)
    qr_size = max(1, round(qr_size * qr_fraction))

    grid_snap = placement.get("grid_snap")
    slot_luminance = placement.get("slot_luminance")

    # rotation_deg: clockwise degrees the QR host area is tilted; clamp to [-45, 45]
    try:
        rotation_deg = float(placement.get("rotation_deg", 0))
        rotation_deg = max(-45.0, min(45.0, rotation_deg))
    except (TypeError, ValueError):
        rotation_deg = 0.0

    return {
        "qr_x": qr_x,
        "qr_y": qr_y,
        "qr_size": qr_size,
        "fit_mode": fit_mode,
        "grid_snap": grid_snap,
        "has_qr_rect": qr_rect is not None,
        "slot_luminance": slot_luminance,
        "rotation_deg": rotation_deg,
    }


# ---------------------------------------------------------------------------
# Core composition — QR is NEVER pixel-modified (color/content)
# ---------------------------------------------------------------------------

def composite(
    poster_path: Path,
    qr_path: Path,
    placement: dict[str, Any],
    out_path: Path,
    *,
    fit_mode_override: str | None = None,
    padding: int | None = None,
    border_radius: int = 12,
    add_drop_shadow: bool = True,
    qr_fraction: float = 1.0,
    card_color: str = "#ffffff",
    dry_run: bool = False,
) -> None:
    Image, ImageDraw, ImageFilter = _require_pillow()

    # --- Load poster (the final image without QR) ---
    try:
        poster = Image.open(poster_path).convert("RGBA")
    except FileNotFoundError:
        print(f"ERROR: poster image not found: {poster_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot open poster image: {exc}", file=sys.stderr)
        sys.exit(4)

    # --- Load original QR — do NOT convert color; keep exact pixels ---
    try:
        qr_src = Image.open(qr_path)
    except FileNotFoundError:
        print(f"ERROR: QR image not found: {qr_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot open QR image: {exc}", file=sys.stderr)
        sys.exit(4)

    if qr_src.mode != "RGBA":
        qr_src = qr_src.convert("RGBA")

    # --- Resolve placement ---
    resolved = _resolve_placement(placement, poster.width, poster.height, qr_fraction)
    qr_x = resolved["qr_x"]
    qr_y = resolved["qr_y"]
    qr_size = resolved["qr_size"]
    fit_mode = fit_mode_override or resolved["fit_mode"]
    grid_snap = resolved["grid_snap"]
    slot_luminance = resolved.get("slot_luminance")
    rotation_deg = resolved.get("rotation_deg", 0.0)
    has_qr_rect = resolved.get("has_qr_rect", False)

    # --- Apply grid snap ONLY for legacy path (no qr_rect) ---
    # When qr_rect is present, compute_qr_rect already applied grid-snap;
    # re-applying here would double-snap, shrinking the QR and shifting position.
    if grid_snap and grid_snap.get("enabled") and not has_qr_rect:
        snapped_size = grid_snap.get("snapped_size_px") or grid_snap.get("snapped_size")
        if snapped_size and snapped_size > 0:
            delta = snapped_size - qr_size
            qr_x -= delta // 2
            qr_y -= delta // 2
            qr_size = snapped_size

    # Clamp to poster bounds
    qr_x = max(0, min(qr_x, poster.width - qr_size))
    qr_y = max(0, min(qr_y, poster.height - qr_size))

    if dry_run:
        print("=== qr_composite dry-run ===")
        print(f"  poster     : {poster_path}  ({poster.width}x{poster.height})")
        print(f"  qr source  : {qr_path}  ({qr_src.width}x{qr_src.height}, mode={qr_src.mode})")
        print(f"  fit_mode   : {fit_mode}")
        print(f"  qr target  : x={qr_x}, y={qr_y}, size={qr_size}px")
        print(f"  rotation   : {rotation_deg}°")
        print(f"  grid_snap  : {grid_snap}")
        print(f"  slot_lum   : {slot_luminance}")
        print(f"  output     : {out_path}")
        return

    # --- Scale QR with NEAREST (pixel-perfect, no anti-aliasing) ---
    qr_w, qr_h = qr_src.size
    ratio = min(qr_size / qr_w, qr_size / qr_h)
    new_w = max(1, round(qr_w * ratio))
    new_h = max(1, round(qr_h * ratio))
    qr_resized = qr_src.resize((new_w, new_h), Image.NEAREST)

    # --- Rotate QR to match host area angle (clockwise = negative in Pillow) ---
    # expand=True ensures the rotated image is not clipped; NEAREST preserves modules.
    # After rotation the canvas grows; we scale it back down so the rotated QR
    # still fits within qr_size (the slot interior dimension).
    if abs(rotation_deg) >= 0.5:
        qr_resized = qr_resized.rotate(-rotation_deg, resample=Image.NEAREST, expand=True)
        rot_w, rot_h = qr_resized.size
        # Scale rotated image back so its longer side == qr_size
        fit_ratio = qr_size / max(rot_w, rot_h)
        fit_w = max(1, round(rot_w * fit_ratio))
        fit_h = max(1, round(rot_h * fit_ratio))
        qr_resized = qr_resized.resize((fit_w, fit_h), Image.NEAREST)
        new_w, new_h = fit_w, fit_h

    result = poster.copy()

    if fit_mode == "detected_slot":
        # --- Direct paste into detected slot, no card tray ---
        # Check if luminance is too low for good contrast
        region_lum = slot_luminance
        if region_lum is None:
            region_lum = _region_mean_luminance(result, qr_x, qr_y, qr_size, qr_size)

        if region_lum < 200:
            # Add minimal white underlay for contrast
            underlay = Image.new("RGBA", (qr_size, qr_size), (255, 255, 255, 255))
            result.paste(underlay, (qr_x, qr_y), mask=underlay.split()[3])

        # Center QR within the target area
        paste_x = qr_x + (qr_size - new_w) // 2
        paste_y = qr_y + (qr_size - new_h) // 2
        result.paste(qr_resized, (paste_x, paste_y), mask=qr_resized.split()[3])

        print(f"OK  Composited QR (detected_slot) -> {out_path}")
        print(f"    qr: x={paste_x} y={paste_y} size={new_w}x{new_h}  region_lum={region_lum:.0f}")

    elif fit_mode == "soft_slot":
        # --- Rounded white underlay covering the inner_rect, then QR centered ---
        # Use inner_rect from placement so the white fill matches the AI-drawn
        # hosting area instead of being a hard-cut square around the QR body.
        inner = placement.get("inner_rect") or {}
        ul_x = inner.get("x", qr_x)
        ul_y = inner.get("y", qr_y)
        ul_w = inner.get("w", qr_size)
        ul_h = inner.get("h", qr_size)
        # Clamp to poster bounds
        ul_x = max(0, min(ul_x, poster.width - ul_w))
        ul_y = max(0, min(ul_y, poster.height - ul_h))

        # Build rounded white underlay at inner_rect size
        ul_radius = max(4, min(ul_w, ul_h) // 16)
        underlay = Image.new("RGBA", (ul_w, ul_h), (0, 0, 0, 0))
        ul_mask = make_rounded_mask(ul_w, ul_h, ul_radius)
        region_lum = slot_luminance
        if region_lum is None:
            region_lum = _region_mean_luminance(result, ul_x, ul_y, ul_w, ul_h)
        # Use semi-transparent if region already bright, solid if dark
        alpha = 160 if region_lum >= 200 else 255
        ul_bg = Image.new("RGBA", (ul_w, ul_h), (255, 255, 255, alpha))
        underlay.paste(ul_bg, mask=ul_mask)
        # Rotate underlay together with QR so the white fill matches the tilted slot
        if abs(rotation_deg) >= 0.5:
            underlay = underlay.rotate(-rotation_deg, resample=Image.BICUBIC, expand=True)
            # Re-center paste position after underlay expand
            ul_paste_x = ul_x - (underlay.width - ul_w) // 2
            ul_paste_y = ul_y - (underlay.height - ul_h) // 2
        else:
            ul_paste_x, ul_paste_y = ul_x, ul_y
        result.paste(underlay, (ul_paste_x, ul_paste_y), mask=underlay.split()[3])

        # Center QR within the underlay's actual paste region
        paste_x = ul_paste_x + (underlay.width - new_w) // 2
        paste_y = ul_paste_y + (underlay.height - new_h) // 2
        result.paste(qr_resized, (paste_x, paste_y), mask=qr_resized.split()[3])

        print(f"OK  Composited QR (soft_slot) -> {out_path}")
        print(f"    underlay: x={ul_x} y={ul_y} size={ul_w}x{ul_h} radius={ul_radius} alpha={alpha}")
        print(f"    qr: x={paste_x} y={paste_y} size={new_w}x{new_h}  region_lum={region_lum:.0f}")

    else:
        # --- fallback_card: original card tray behavior ---
        if padding is None:
            padding = max(10, qr_size // 8)

        card_size = qr_size + 2 * padding
        card_x = qr_x - padding
        card_y = qr_y - padding
        card_x = max(0, min(card_x, poster.width - card_size))
        card_y = max(0, min(card_y, poster.height - card_size))

        card_rgba = hex_to_rgba(card_color)
        card = Image.new("RGBA", (card_size, card_size), (0, 0, 0, 0))
        card_bg = Image.new("RGBA", (card_size, card_size), card_rgba)
        rounded_mask = make_rounded_mask(card_size, card_size, border_radius)
        card.paste(card_bg, mask=rounded_mask)

        qr_offset_x = (card_size - new_w) // 2
        qr_offset_y = (card_size - new_h) // 2
        card.paste(qr_resized, (qr_offset_x, qr_offset_y), mask=qr_resized.split()[3])

        if add_drop_shadow:
            result = add_shadow(result, card_x, card_y, card_size, card_size)

        result.paste(card, (card_x, card_y), mask=card.split()[3])

        print(f"OK  Composited QR (fallback_card) -> {out_path}")
        print(f"    card: x={card_x} y={card_y} size={card_size}px  qr_body: {new_w}x{new_h}")

    # --- Save: always PNG for QR integrity ---
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Force PNG to avoid JPEG compression damaging QR edges
    if out_path.suffix.lower() in {".jpg", ".jpeg"}:
        print("    WARNING: Output requested as JPEG; forcing PNG for QR integrity")
        out_path = out_path.with_suffix(".png")
    result.save(out_path, "PNG")

    print(f"    fit_mode: {fit_mode}  scale: NEAREST  format: PNG")
    print(f"    source: {placement.get('_source', 'unknown')}  anchor: {placement.get('anchor', '?')}")
    print("    QR content: scale only (NEAREST), no recolor, no redraw, PNG only")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Composite original QR code onto a poster PNG using qr_placement.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--poster", required=True, help="Path to final poster image WITHOUT QR (PNG/JPEG)")
    parser.add_argument("--qr", required=True, help="Path to original QR code image — will NOT be recolored")
    parser.add_argument("--placement", required=True, help="Path to qr_placement.json")
    parser.add_argument("--out", required=True, help="Output path for composited final image")
    parser.add_argument("--fit-mode", choices=["detected_slot", "soft_slot", "fallback_card", "auto"],
                        default="auto", help="Composition mode (default: auto = read from placement JSON)")
    parser.add_argument("--padding", type=int, default=None, help="Padding inside card tray for fallback_card (default: auto)")
    parser.add_argument("--border-radius", type=int, default=12, help="Card tray corner radius (default: 12)")
    parser.add_argument("--qr-fraction", type=float, default=1.0, help="Multiplier for requested QR body size (default: 1.0)")
    parser.add_argument("--card-color", default="#ffffff", help="Card tray background hex color (default: #ffffff)")
    shadow_group = parser.add_mutually_exclusive_group()
    shadow_group.add_argument("--shadow", dest="shadow", action="store_true", default=True, help="Add drop shadow (default)")
    shadow_group.add_argument("--no-shadow", dest="shadow", action="store_false", help="Disable drop shadow")
    parser.add_argument("--dry-run", action="store_true", help="Print parameters without writing output")

    # Keep --bg as hidden alias for --poster for backward compatibility
    parser.add_argument("--bg", dest="poster_alias", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    poster_arg = args.poster_alias or args.poster
    poster_path = Path(poster_arg).expanduser().resolve()
    qr_path = Path(args.qr).expanduser().resolve()
    placement_path = Path(args.placement).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    if not args.dry_run:
        if not poster_path.exists():
            print(f"ERROR: poster image not found: {poster_path}", file=sys.stderr)
            return 1
        if not qr_path.exists():
            print(f"ERROR: QR image not found: {qr_path}", file=sys.stderr)
            return 1

    placement = load_placement(placement_path)

    fit_mode_override = None if args.fit_mode == "auto" else args.fit_mode

    try:
        composite(
            poster_path=poster_path,
            qr_path=qr_path,
            placement=placement,
            out_path=out_path,
            fit_mode_override=fit_mode_override,
            padding=args.padding,
            border_radius=args.border_radius,
            add_drop_shadow=args.shadow,
            qr_fraction=args.qr_fraction,
            card_color=args.card_color,
            dry_run=args.dry_run,
        )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: composition failed: {exc}", file=sys.stderr)
        return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
