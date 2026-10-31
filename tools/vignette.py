"""Retone-don't-cut vignette cutter: vignette.py <candidate dir>  (see CUTTING.md)

Reads <dir>/crop.png (plate.py's crop of the approved scan) and <dir>/params.json, writes
<dir>/cut.png in plate.py's format (RGBA, crop coordinates, halo-toned soft edge) plus
check.jpg and debug images. finish.py then makes final.png as usual.

Why not plate.py's paper flood: it decides paper by one brightness threshold, so pale
leaves, blossom and white tail tips are "paper" and get eaten, and a painted sky cannot be
dropped at all without a hand trace. Here nothing drawn is cut:

1. The page (or a painted sky, inside `zone`) is estimated as a smooth field from the
   pixels that look like it, interpolated under the ink, and divided out, so it lands on
   the halo tone wherever it is. That is a per-pixel white balance, not a mask.
2. What still differs from the halo tone is the drawing. Faint residue (paper grain,
   show-through text) fades out through a soft matte; anything enclosed by ink is left raw.
3. The outline is that drawing plus a margin, then plate.py's own halo.

params: seeds (crop px, one per detached part), balance (paper RGB, used outside zone),
zone (polygons where the background is flattened; default everywhere), protect (polygons
never sampled as background and never matted: pale washes that are subject; their
corners are rounded `round` times), drop
(polygons removed outright: caption, signatures), fade ([polygon, px] as in plate.py),
enclosed pockets always kept and page-toned (PR #322),
t0/t1 (matte thresholds), reach (px from ink within which faint tone joined to it is
kept), tol (final background tolerance), sigma, margin, close.
"""

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

PAPER = np.array([0xF0, 0xEC, 0xE5], dtype=np.float32)
HALO_SHIPPED = 17
SCALE = 8  # the background field is smooth, so it is solved at 1/8 size


def region(polygons, size):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for polygon in polygons:
        draw.polygon([tuple(p) for p in polygon], fill=255)
    return np.asarray(mask) > 0


def rounded(polygon, times):
    """Chaikin corner cutting: a hand polygon's facets would show in the halo's outline."""
    for _ in range(times):
        pairs = zip(polygon, polygon[1:] + polygon[:1], strict=True)
        polygon = [
            [a[0] * (1 - t) + b[0] * t, a[1] * (1 - t) + b[1] * t]
            for a, b in pairs
            for t in (0.25, 0.75)
        ]
    return polygon


def as_mask(mask):
    return Image.fromarray((mask * 255).astype(np.uint8), "L")


def grow(mask, r):
    return np.asarray(as_mask(mask).filter(ImageFilter.MaxFilter(2 * r + 1))) > 0 if r else mask


def shrink(mask, r):
    return np.asarray(as_mask(mask).filter(ImageFilter.MinFilter(2 * r + 1))) > 0 if r else mask


def soften(mask, sigma):
    return np.asarray(as_mask(mask).filter(ImageFilter.GaussianBlur(sigma)), np.float32) / 255


def flood(mask, seed):
    if not mask[seed[1], seed[0]]:
        print(f"  ! seed {list(seed)} is not inside the mask, skipped")
        return np.zeros_like(mask)
    image = as_mask(mask).copy()
    ImageDraw.floodfill(image, tuple(seed), 128)
    return np.asarray(image) == 128


def reduce(a):
    h, w = a.shape[:2]
    H, W = -(-h // SCALE), -(-w // SCALE)
    pad = [(0, H * SCALE - h), (0, W * SCALE - w)] + [(0, 0)] * (a.ndim - 2)
    a = np.pad(a, pad, mode="edge")
    return a.reshape(H, SCALE, W, SCALE, *a.shape[2:]).mean(axis=(1, 3))


def gblur(a, sigma):
    r = max(1, int(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    for axis in (0, 1):
        pad = [(r, r) if i == axis else (0, 0) for i in range(a.ndim)]
        p = np.pad(a, pad, mode="reflect" if a.shape[axis] > r else "edge")
        out = np.zeros_like(a)
        for i, weight in enumerate(k):
            out += weight * np.take(p, range(i, i + a.shape[axis]), axis=axis)
        a = out
    return a


def field(px, mask, sigma):
    """Normalized convolution: the mean of masked pixels, wider where few are near."""
    values, weight = reduce(px * mask[..., None]), reduce(mask.astype(np.float32))
    out, have = None, None
    for s in (sigma, sigma * 2, sigma * 4, sigma * 10):
        w = gblur(weight, s / SCALE)
        est = gblur(values, s / SCALE) / np.maximum(w, 1e-6)[..., None]
        ok = np.clip((w - 0.02) / 0.08, 0, 1)[..., None]
        if out is None:
            out, have = est * ok, ok
        else:
            out, have = out + est * ok * (1 - have), have + ok * (1 - have)
    out = out / np.maximum(have, 1e-6)
    h, w_ = px.shape[:2]
    full = [
        np.asarray(
            Image.fromarray(out[..., c].astype(np.float32), "F").resize(
                (w_, h), Image.Resampling.BICUBIC
            )
        )
        for c in range(3)
    ]
    return np.stack(full, axis=-1)


def main(folder):
    p = json.loads((folder / "params.json").read_text())
    px = np.asarray(Image.open(folder / "crop.png").convert("RGB"), np.float32)
    h, w = px.shape[:2]
    size = (w, h)
    zone = region(p["zone"], size) if "zone" in p else np.ones((h, w), bool)
    protect = region([rounded(poly, p.get("round", 3)) for poly in p.get("protect", [])], size)
    dropped = region(p.get("drop", []), size)
    balance = np.array(p["balance"], np.float32)

    # 1. the background field, by sigma-clipping toward what the page looks like locally
    sample = zone & ~protect & ~dropped
    B = np.broadcast_to(np.median(px[sample], axis=0), px.shape)
    sigma = p.get("sigma", 40)
    steps = (45, 5 * sigma), (28, 2.5 * sigma), (18, 1.5 * sigma), (p.get("tol", 12), sigma)
    for tol, s in steps:
        near = np.abs(px - B).max(axis=2) < tol
        B = field(px, near & sample, s)
    background = near & sample
    gain = np.broadcast_to(PAPER / balance, px.shape).copy()
    local = np.clip(PAPER / np.maximum(B, 1), 0.7, 1.6)
    blend = soften(zone, 2)[..., None]
    gain = gain * (1 - blend) + local * blend
    flat = np.clip(px * gain, 0, 255)

    # 2. the drawing: whatever is not page tone now
    D = np.abs(flat - PAPER).max(axis=2)
    noise = float(np.percentile(D[background], 95))
    t0 = p.get("t0", noise + 1)
    t1 = p.get("t1", t0 + 10)
    print(f"background {background.mean():.0%} of crop, residue p95 {noise:.1f}", end="")
    print(f", matte {t0:.1f}-{t1:.1f}")
    ink = (D > t1) & ~dropped
    # a speck of foxing is not drawing: ink has to be dense over a few px to count
    ink &= soften(ink, p.get("speck", 2.5)) > 0.45
    # Enclosed by ink and not page-coloured (a white belly, a pale cheek) is left raw. Enclosed
    # page between leaves still goes through the matte, or show-through text prints there.
    sealed = grow(ink | protect, p.get("seal", 2))
    outside = flood(np.pad(~sealed, 1, constant_values=True), (0, 0))[1:-1, 1:-1]
    solid = (~outside | ink | protect) & ~dropped
    raw = ((~outside & ~background) | ink | protect) & ~dropped
    # Faint tone joined to the drawing is drawing: a white throat's edge, the pale rim of a
    # blossom, a dry-brush stroke running past a twig's end. Only weak tone with no path to
    # ink (foxing, show-through) still fades (maintainer, PR #348).
    weak = (soften((D > t0) & ~dropped, 1) > 0.5) & grow(ink, p.get("reach", 48))
    attached = np.zeros((h, w), bool)
    for seed in p["seeds"]:
        attached |= flood(weak | ink, seed)
    attached &= weak & ~ink
    raw |= attached
    solid |= attached
    matte = np.clip((D - t0) / (t1 - t0), 0, 1)
    matte = np.maximum(matte, soften(shrink(raw, 1), 1.5))
    matte[dropped] = 0

    # 3. the outline: the drawing reachable from the seeds, with a margin
    close = p.get("close", 12)
    joined = shrink(grow(solid, close), close) | solid
    subject = np.zeros((h, w), bool)
    for seed in p["seeds"]:
        subject |= flood(joined, seed)
    lost = int((solid & ~subject).sum())
    kept = solid & subject
    print(f"drawing {int(kept.sum())} px of {w * h}; {lost} px not connected to a seed")
    envelope = grow(kept, p.get("margin", 4))
    envelope |= shrink(grow(envelope, close), close)  # no notches between neighbouring leaves
    envelope = (
        soften(envelope, p.get("smooth", 5)) > 0.45
    )  # a calm outline; the halo hides the rest
    # Every pocket the outline encloses is kept and page-toned, never cut open: the maintainer
    # page-toned an open pocket himself on PR #322 ("so the twig is whole"), and upstream's
    # plate.py keeps enclosed paper too. The page there is already retoned to the halo tone.
    # Filled at full size, so a pocket's edge is the drawing's own, never a staircase.

    def filled(mask):
        return ~flood(np.pad(~mask, 1, constant_values=True), (0, 0))[1:-1, 1:-1]

    envelope = filled(envelope)

    rows, cols = np.flatnonzero(envelope.any(1)), np.flatnonzero(envelope.any(0))
    longest = max(rows[-1] - rows[0], cols[-1] - cols[0])
    halo_px = float(HALO_SHIPPED * max(1.0, longest / 1200))
    soft = as_mask(envelope).filter(ImageFilter.GaussianBlur(1.5))
    for polygon, fpx in p.get("fade", []):
        faded = as_mask(envelope).filter(ImageFilter.MinFilter(2 * fpx + 1))
        faded = faded.filter(ImageFilter.GaussianBlur(fpx / 2))
        fzone = as_mask(region([polygon], size)).filter(ImageFilter.GaussianBlur(2 * fpx))
        soft = Image.composite(faded, soft, fzone)
    show = (np.asarray(soft, np.float32) / 255 * matte)[..., None]
    rgb = PAPER + show * (flat - PAPER)
    grown = np.asarray(as_mask(envelope).filter(ImageFilter.GaussianBlur(halo_px / 2))) > 6
    grown = filled(grown)  # the halo can close a pocket's mouth and leave the pocket
    alpha = as_mask(grown).filter(ImageFilter.GaussianBlur(0.8))

    result = Image.fromarray(rgb.round().astype(np.uint8), "RGB").convert("RGBA")
    result.putalpha(alpha)
    result.save(folder / "cut.png")
    check = Image.new("RGB", size, (70, 110, 160))
    check.paste(result, (0, 0), result)
    check.save(folder / "check.png")
    small = check.copy()
    small.thumbnail((1500, 1500))
    small.save(folder / "check.jpg", quality=90)
    # debug: the flattened scan, and what was judged drawing (white), dropped ink (red)
    Image.fromarray(flat.round().astype(np.uint8), "RGB").save(
        folder / "debug-flat.jpg", quality=88
    )
    debug = np.zeros((h, w, 3), np.uint8)
    debug[envelope] = (60, 60, 60)
    debug[kept] = (255, 255, 255)
    debug[solid & ~subject] = (255, 0, 0)
    debug[background & ~envelope] = (0, 60, 0)
    Image.fromarray(debug).save(folder / "debug-mask.png")
    stats = {
        "background_fraction": float(background.mean()),
        "residue_p95": noise,
        "matte": [float(t0), float(t1)],
        "drawing_px": int(kept.sum()),
        "unconnected_px": lost,
        "halo_px": halo_px,
    }
    (folder / "cut-stats.json").write_text(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
