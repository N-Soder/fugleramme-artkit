"""Hold finish.py's deepening to the scan's own colourfulness: tone.py <candidate dir> [mode]

finish.py deepens a faded scan with a black point and gamma, which also stretches every
colour away from grey. On a strongly coloured bird that reads oversaturated beside the
plate it came from (PR 261, Eastern Rosella). This scales colour back toward grey by one
factor, so the mean saturation of the drawn pixels equals the balanced scan's again; the
deepened darks stay. Reads finish.py's output from final-finish.png, writes final.png,
and records the factor in tone.json. A factor of 1 or more changes nothing.
"""

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

PAPER = np.array([0xF0, 0xEC, 0xE5], dtype=np.float64)
LUMA = np.array([0.299, 0.587, 0.114])


def saturation(rgb, mode):
    spread = rgb.max(axis=1) - rgb.min(axis=1)
    return spread if mode == "chroma" else spread / np.maximum(rgb.max(axis=1), 1)


folder, mode = Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "saturation"
source = folder / "final-finish.png"
final = np.asarray(Image.open(source).convert("RGBA")).astype(np.float64)
cut = Image.open(folder / "cut.png").convert("RGBA")
scan = np.asarray(
    cut.crop(cut.getchannel("A").getbbox()).resize(final.shape[1::-1], Image.Resampling.BOX)
)
drawn = (
    (final[..., 3] == 255)
    & (scan[..., 3] == 255)
    & (np.abs(scan[..., :3] - PAPER).max(axis=2) > 12)
)
before = saturation(scan[..., :3][drawn].astype(np.float64), mode).mean()
after = saturation(final[..., :3][drawn], mode).mean()
factor = min(1.0, before / after)
rgb = final[..., :3]
grey = (rgb @ LUMA)[..., None]
toned = np.clip(grey + factor * (rgb - grey), 0, 255)
toned[final[..., 3] < 255] = PAPER
out = np.concatenate([toned.round(), final[..., 3:]], axis=-1).astype(np.uint8)
Image.fromarray(out, "RGBA").save(folder / "final.png")
record = {
    "mode": mode,
    "scan": round(float(before), 4),
    "finished": round(float(after), 4),
    "factor": round(factor, 4),
}
(folder / "tone.json").write_text(json.dumps(record, indent=2))
print(record)
