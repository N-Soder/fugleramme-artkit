"""Pre-flight checks on a recut candidate: qa.py <candidate dir>

Each check is one way a PR #261 plate was sent back (see CUTTING.md). It judges the drawing
from crop.png on its own terms, not from the cutter's mask, so a cutter fault cannot hide
itself:

- box: drawing touching the crop's edge, meaning the box clipped the printed image (rule 2).
- lost: drawing in the crop that the cut left transparent: eaten leaves, blossom, tail tips
  (rules 1 and 4). Strong ink and pale wash are reported apart, and `drop` polygons in
  params.json are intended removals, not losses. lost.jpg marks every blob.
- erased: drawing the cut kept opaque but painted over in page tone. vignette.py fades faint
  drawing toward the page colour rather than making it transparent, so `lost` cannot see it:
  a white chin by the bill, pale blossom, a dry twig end, a pale breast whitened by the
  retone (maintainer, PR #348). A faded spot with no path to ink is foxing, reported apart
  without failing. erased.jpg sets each spot's scan beside its cut and beside
  the final at the shipped size.
- colour: mean saturation of the final against the scan (rule 5).
- pockets: transparent holes inside the outline of final.png. An enclosed pocket is page-toned,
  never cut open (maintainer, PR #322; upstream plate.py keeps enclosed paper too).
- floating: more than one opaque piece in final.png, a leaf or twig left as its own island
  (maintainer, PR #124: "no floating leaves, twigs or perches").
- fringe: dark pixels under the soft edge of final.png, as upstream's ringscan (must be 0).
  Small spots well clear of the outline are foxing, reported as `specks` without failing; a
  loss touching the outline (a clipped tail tip, an eaten leaf edge) always fails.
- unconnected: drawing vignette.py did not reach from a seed (rule 6). A warning only: the
  lost check judges what those pixels were.

Writes qa.json, lost.jpg, erased.jpg and disclosure.md (the retouching a PR must name, rule 8). Exits 1
if any check fails. The checks back up a full-size look; they never replace one.
"""

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

LIMITS = {
    "box_run_px": 12,  # drawing along one edge for longer than this means a clipped box
    "lost_strong_px": 40,  # a lost blob of ink bigger than this fails
    "lost_pale_px": 400,  # pale wash is noisier; a blob this big is a lost leaf or blossom
    "erased_rise": 4,  # faint drawing: this far above the page's own noise
    "erased_keep": 0.5,  # the cut must keep at least this share of the drawing's contrast
    "erased_px": 100,  # an erased blob this big fails: a chin, a blossom, a twig end
    "erased_ink": 20,  # drawing an erased spot must touch to count; on its own it is foxing
    "saturation_ratio": 1.08,
    "fringe_dark_px": 0,
    "pocket_px": 0,
    "speck_px": 1500,  # foxing: smaller than this and well clear of the drawing
    "speck_clear_px": 40,  # how far a spot must sit from the kept outline to count as foxing
}


def region(polygons, size):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for polygon in polygons:
        draw.polygon([tuple(p) for p in polygon], fill=255)
    return np.asarray(mask) > 0


def as_mask(mask):
    return Image.fromarray((mask * 255).astype(np.uint8), "L")


def gblur(a, sigma):
    r = max(1, int(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    for axis in (0, 1):
        pad = [(r, r) if i == axis else (0, 0) for i in range(a.ndim)]
        p = np.pad(a, pad, mode="edge")
        a = sum(
            weight * np.take(p, range(i, i + a.shape[axis]), axis=axis)
            for i, weight in enumerate(k)
        )
    return a


def page_estimate(px, page=None):
    """The local page tone, from pixels that look like page only (normalized convolution).

    A plain blur or max filter is dragged dark beside a large dark bird and then reports the
    clean page next to it as lost wash. Paper is sampled, interpolated under the drawing,
    and re-sampled once against that estimate, so a darker gutter is still page.

    `page` fixes where to sample instead, such as where the scan had page.
    """
    h, w = px.shape[:2]
    small = np.asarray(Image.fromarray(px.astype(np.uint8), "RGB").reduce(8), np.float32)
    if page is not None:
        page = np.asarray(as_mask(page).reduce(8)) > 230
    luma = small.mean(axis=2)
    ref = np.median(small[page if page is not None else luma >= np.percentile(luma, 70)], axis=0)
    est = np.broadcast_to(ref, small.shape)
    for tol, sigma in ((30, 12), (14, 6)):
        if page is not None:
            near = page.astype(np.float32)
        else:
            near = (np.abs(small - est).max(axis=2) < tol).astype(np.float32)
        weight = gblur(near, sigma)
        local = gblur(small * near[..., None], sigma) / np.maximum(weight, 1e-6)[..., None]
        trust = np.clip(weight / 0.1, 0, 1)[..., None]
        est = local * trust + est * (1 - trust)
    full = [
        np.asarray(
            Image.fromarray(est[..., c].astype(np.float32), "F").resize(
                (w, h), Image.Resampling.BICUBIC
            )
        )
        for c in range(3)
    ]
    return np.stack(full, axis=-1)


def dense(mask, sigma=2.0, share=0.45):
    """Paper grain and foxing are specks; drawing is dense over a few px."""
    return np.asarray(as_mask(mask).filter(ImageFilter.GaussianBlur(sigma))) > 255 * share


def blobs(mask, minimum):
    """Connected blobs at quarter size (PIL's flood is pure Python): [(px, bbox), ...]."""
    q = 4
    h, w = mask.shape
    small = np.pad(mask, [(0, -h % q), (0, -w % q)]).reshape(-(-h // q), q, -(-w // q), q)
    count = small.sum(axis=(1, 3))
    image = as_mask(count > 0).copy()
    found = []
    for y, x in zip(*np.nonzero(count), strict=True):
        if image.getpixel((int(x), int(y))) != 255:
            continue
        ImageDraw.floodfill(image, (int(x), int(y)), 100)
        part = np.asarray(image) == 100
        size = int(count[part].sum())
        if size >= minimum:
            ys, xs = np.nonzero(part)
            found.append(
                (
                    size,
                    [
                        int(xs.min()) * q,
                        int(ys.min()) * q,
                        int(xs.max() + 1) * q,
                        int(ys.max() + 1) * q,
                    ],
                )
            )
        image.paste(50, mask=as_mask(part))
    return sorted(found, reverse=True)


def longest_run(values):
    best = run = 0
    for v in values:
        run = run + 1 if v else 0
        best = max(best, run)
    return best


SHIPPED_PX = 1200  # the long side of the image the app shows, and the maintainer reviews


def erased_sheet(folder, crop, cut, boxes, pad=60, tile=360):
    """Each erased spot: the scan and the cut on blue at full size, then the final as shipped.

    The third column is final.png shrunk to SHIPPED_PX on the page tone and blown back up, so
    a fade shows as the maintainer sees it (CUTTING.md rule 18).
    """
    scan = Image.fromarray(crop.astype(np.uint8), "RGB")
    cut_image = Image.fromarray(cut, "RGBA")
    shipped = None
    final_path = folder / "final.png"
    box = cut_image.getchannel("A").getbbox()
    if final_path.exists() and box:
        final = Image.open(final_path).convert("RGBA")
        scale = SHIPPED_PX / max(final.size)
        small = final.resize(
            (round(final.width * scale), round(final.height * scale)), Image.Resampling.LANCZOS
        )
        shipped = Image.new("RGB", small.size, (0xF0, 0xEC, 0xE5))
        shipped.paste(small, (0, 0), small)
        to_shipped = small.width / (box[2] - box[0])
    rows = []
    for n, (x0, y0, x1, y1) in enumerate(boxes, 1):
        area = (max(0, x0 - pad), max(0, y0 - pad), x1 + pad, y1 + pad)
        tiles = [scan.crop(area)]
        piece = cut_image.crop(area)
        tiles.append(Image.new("RGB", piece.size, (70, 110, 160)))
        tiles[1].paste(piece, (0, 0), piece)
        if shipped is not None:
            spot = [
                round((v - o) * to_shipped)
                for v, o in zip(area, (box[0], box[1], box[0], box[1]), strict=True)
            ]
            tiles.append(shipped.crop(spot).resize(piece.size, Image.Resampling.NEAREST))
        for image in tiles:
            image.thumbnail((tile, tile))
        width = tiles[0].width
        row = Image.new("RGB", (max(len(tiles) * (width + 10), 300), tiles[0].height + 20), "white")
        for i, image in enumerate(tiles):
            row.paste(image, (i * (width + 10), 20))
        label = "scan | cut | shipped" if shipped is not None else "scan | cut"
        ImageDraw.Draw(row).text((4, 4), f"{n}  {label}  {area}", fill=(200, 0, 0))
        rows.append(row)
    if not rows:
        rows.append(Image.new("RGB", (2 * tile + 10, 40), "white"))
        ImageDraw.Draw(rows[0]).text((4, 12), "nothing erased", fill=(0, 120, 0))
    sheet = Image.new("RGB", (max(r.width for r in rows), sum(r.height for r in rows)), "white")
    y = 0
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.height
    sheet.save(folder / "erased.jpg", quality=88)


def saturation(rgb):
    return (rgb.max(axis=1) - rgb.min(axis=1)) / np.maximum(rgb.max(axis=1), 1)


def main(folder):
    spec = folder / "params.json"
    params = json.loads(spec.read_text()) if spec.exists() else {}
    crop = np.asarray(Image.open(folder / "crop.png").convert("RGB"), np.float32)
    cut = np.asarray(Image.open(folder / "cut.png").convert("RGBA"))
    h, w = crop.shape[:2]
    if cut.shape[:2] != (h, w):
        sys.exit(f"cut.png {cut.shape[1]}x{cut.shape[0]} is not crop-sized {w}x{h}")
    kept = cut[..., 3] > 128
    dropped = region(params.get("drop", []), (w, h))

    D = np.abs(page_estimate(crop) - crop).max(axis=2)
    noise = float(np.percentile(D[~kept & ~dropped], 90)) if (~kept & ~dropped).any() else 8.0
    strong = dense(D > noise + 30) & ~dropped
    pale = dense(D > noise + 10, sigma=3, share=0.6) & ~strong & ~dropped

    results = {}
    edge = 3
    sides = {
        "top": strong[:edge].any(0),
        "bottom": strong[-edge:].any(0),
        "left": strong[:, :edge].any(1),
        "right": strong[:, -edge:].any(1),
    }
    runs = {side: longest_run(v) for side, v in sides.items()}
    results["box"] = {"ok": max(runs.values()) <= LIMITS["box_run_px"], "longest_run_px": runs}

    # "lost" is drawing well outside the kept outline; the outline's own margin is not a loss
    outside = ~np.asarray(as_mask(kept).filter(ImageFilter.MaxFilter(5))).astype(bool)
    q = 4
    small_kept = (
        as_mask(kept)
        .reduce(q)
        .filter(ImageFilter.MaxFilter(2 * (LIMITS["speck_clear_px"] // q) + 1))
    )
    near = np.asarray(small_kept.resize((w, h))) > 0

    def split(found, mask):
        losses, specks = [], []
        for size, (x0, y0, x1, y1) in found:
            touches = bool((mask[y0:y1, x0:x1] & near[y0:y1, x0:x1]).any())
            (losses if touches or size >= LIMITS["speck_px"] else specks).append(
                (size, [x0, y0, x1, y1])
            )
        return losses, specks

    lost_strong, specks_strong = split(
        blobs(strong & outside, LIMITS["lost_strong_px"]), strong & outside
    )
    lost_pale, specks_pale = split(blobs(pale & outside, LIMITS["lost_pale_px"]), pale & outside)
    specks = sorted(specks_strong + specks_pale, reverse=True)
    results["lost"] = {
        "ok": not lost_strong and not lost_pale,
        "strong": [{"px": s, "bbox": b} for s, b in lost_strong[:12]],
        "pale": [{"px": s, "bbox": b} for s, b in lost_pale[:12]],
        "specks": [{"px": s, "bbox": b} for s, b in specks[:24]],
        "noise_p90": round(noise, 1),
    }

    view = Image.fromarray(crop.astype(np.uint8), "RGB")
    view = Image.blend(view, Image.new("RGB", view.size, (255, 255, 255)), 0.45)
    shade = np.zeros((h, w, 4), np.uint8)
    shade[strong & outside] = (230, 0, 160, 255)
    shade[pale & outside] = (255, 140, 0, 255)
    view.paste(Image.fromarray(shade, "RGBA"), (0, 0), Image.fromarray(shade, "RGBA"))
    draw = ImageDraw.Draw(view)
    for n, (_, (x0, y0, x1, y1)) in enumerate(lost_strong[:12] + lost_pale[:12], 1):
        draw.rectangle([x0 - 8, y0 - 8, x1 + 8, y1 + 8], outline=(200, 0, 0), width=4)
        draw.text((x0 - 8, y0 - 22), str(n), fill=(200, 0, 0))
    view.thumbnail((2000, 2000))
    view.save(folder / "lost.jpg", quality=88)

    # "erased" is drawing still inside the outline that the cut painted over in page tone.
    # A spot with no path to ink is foxing and may go; one joined to ink is drawing (#348).
    cut_rgb = cut[..., :3].astype(np.float32)
    # Judged against two page estimates, and erased only if both agree: the cut's own page
    # takes in a pale breast near page tone, while the scan's page positions run thin
    # between dense blossom.
    page = (D < noise) & ~dropped
    kept_contrast = np.maximum(
        np.abs(page_estimate(cut_rgb) - cut_rgb).max(axis=2),
        np.abs(page_estimate(cut_rgb, page) - cut_rgb).max(axis=2),
    )
    rise = noise + LIMITS["erased_rise"]
    faint = dense(D > rise, sigma=3, share=0.3) & ~dropped
    erased = dense(faint & kept & (kept_contrast < LIMITS["erased_keep"] * D), sigma=2, share=0.5)
    ink = dense(D > noise + LIMITS["erased_ink"])
    found, foxing = [], []
    for size, (x0, y0, x1, y1) in blobs(erased, LIMITS["erased_px"]):
        spot = erased[y0:y1, x0:x1]
        if D[y0:y1, x0:x1][spot].mean() < rise:
            continue  # paper grain at the edge of a stroke, not a faded part
        ring = (slice(max(0, y0 - 6), y1 + 6), slice(max(0, x0 - 6), x1 + 6))
        joined = bool((ink[ring] & ~erased[ring]).any())
        (found if joined else foxing).append((size, [x0, y0, x1, y1]))
    results["erased"] = {
        "ok": not found,
        "blobs": [{"px": s, "bbox": b} for s, b in found[:12]],
        "foxing": [{"px": s, "bbox": b} for s, b in foxing[:12]],
    }
    erased_sheet(folder, crop, cut, [b for _, b in (found + foxing)[:12]])

    final_path = folder / "final.png"
    if final_path.exists():
        final = np.asarray(Image.open(final_path).convert("RGBA")).astype(np.float64)
        box = Image.fromarray(cut).getchannel("A").getbbox()
        scan = np.asarray(
            Image.fromarray(cut).crop(box).resize(final.shape[1::-1], Image.Resampling.BOX)
        ).astype(np.float64)
        paper = np.array([0xF0, 0xEC, 0xE5])
        drawn = (final[..., 3] == 255) & (scan[..., 3] == 255)
        drawn &= np.abs(scan[..., :3] - paper).max(axis=2) > 12
        after = saturation(final[..., :3][drawn]).mean()
        ratio = float(after / saturation(scan[..., :3][drawn]).mean())
        results["colour"] = {
            "ok": ratio <= LIMITS["saturation_ratio"],
            "saturation_ratio": round(ratio, 3),
        }
        a = final.astype(int)
        soft = (a[..., 3] >= 25) & (a[..., 3] < 255)
        dark = int((a[..., :3][soft].max(1) < 200).sum())
        results["fringe"] = {"ok": dark <= LIMITS["fringe_dark_px"], "dark_px": dark}
        clear = np.pad(a[..., 3] < 128, 1, constant_values=True)
        reach = as_mask(clear).copy()
        ImageDraw.floodfill(reach, (0, 0), 128)
        pocket = (np.asarray(reach) == 255)[1:-1, 1:-1]
        pieces = blobs(np.asarray(Image.fromarray(a[..., 3].astype(np.uint8))) >= 128, 64)
        results["floating"] = {
            "ok": len(pieces) <= 1,
            "pieces": len(pieces),
            "bbox": [b for _, b in pieces[1:6]],
        }
        results["pockets"] = {
            "ok": int(pocket.sum()) <= LIMITS["pocket_px"],
            "px": int(pocket.sum()),
        }
    else:
        missing = {"ok": False, "missing": "final.png"}
        results["colour"] = results["fringe"] = results["pockets"] = missing
        results["floating"] = missing

    stats_path = folder / "cut-stats.json"
    if stats_path.exists():
        lost = json.loads(stats_path.read_text()).get("unconnected_px", 0)
        results["unconnected"] = {"ok": True, "warn": lost > 0, "px": lost}

    tone_path = folder / "tone.json"
    tone = json.loads(tone_path.read_text()) if tone_path.exists() else {}
    notes = []
    if "zone" in params:
        notes.append(
            "Background (sky or painted ground) inside the marked zone retoned to the page"
            " tone; no subject traced."
        )
    else:
        notes.append(
            "Page retoned to the frame's paper tone by a smooth per-pixel white balance;"
            " nothing thresholded away."
        )
    if params.get("drop"):
        notes.append(
            f"Removed {len(params['drop'])} non-drawing region(s): caption, signatures,"
            " plate marks or specks."
        )
    if tone.get("factor", 1) < 1:
        notes.append(
            f"Colour held to the scan: finish.py's saturation scaled by {tone['factor']:.2f}."
        )
    if params.get("fade"):
        notes.append("Edge faded where a painted wash has no drawn outline.")
    (folder / "disclosure.md").write_text("".join(f"- {n}\n" for n in notes))

    ok = all(r["ok"] for r in results.values())
    (folder / "qa.json").write_text(json.dumps({"ok": ok, "limits": LIMITS, **results}, indent=2))
    for name, r in results.items():
        hidden = ("ok", "strong", "pale", "specks", "bbox", "blobs", "foxing")
        detail = {k: v for k, v in r.items() if k not in hidden}
        if name == "erased":
            detail = {"blobs": len(r["blobs"]), "foxing": len(r["foxing"])}
        if name == "lost":
            detail = {k: len(r[k]) for k in ("strong", "pale", "specks")}
        print(f"  {'ok  ' if r['ok'] else 'FAIL'} {name:12} {json.dumps(detail)}")
    print(
        f"{'PASS' if ok else 'FAIL'} {folder}  (see lost.jpg, erased.jpg, qa.json, disclosure.md)"
    )
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(Path(sys.argv[1])) else 1)
