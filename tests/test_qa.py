"""tools/qa.py catches each way a PR #261 plate was sent back, on synthetic plates.

qa.py needs numpy, which lives in upstream's locked environment, so these run only with
FUGART_TEST_UPSTREAM set (as the real-checkout smoke test does).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

QA = Path(__file__).parents[1] / "tools/qa.py"
PAGE = (232, 224, 205)  # an aged page
SIZE = (600, 500)
BIRD = [200, 150, 420, 330]
LEAF = [460, 360, 540, 430]  # pale wash, brighter than the bird but not page


@pytest.fixture(scope="session")
def art_python(tmp_path_factory):
    upstream = os.environ.get("FUGART_TEST_UPSTREAM")
    if not upstream:
        pytest.skip("set FUGART_TEST_UPSTREAM to run qa.py in upstream's environment")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["UV_PROJECT_ENVIRONMENT"] = str(tmp_path_factory.mktemp("art") / "env")

    def run(folder):
        return subprocess.run(
            ["uv", "run", "--locked", "python", "-B", str(QA), str(folder)],
            cwd=upstream,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )

    return run


def plate(folder, bird=BIRD, leaf=True):
    crop = Image.new("RGB", SIZE, PAGE)
    draw = ImageDraw.Draw(crop)
    draw.ellipse(bird, fill=(90, 70, 50))
    if leaf:
        draw.ellipse(LEAF, fill=(205, 200, 160))
        draw.line([(400, 300), (480, 380)], fill=(150, 130, 100), width=8)  # its stalk
    crop.save(folder / "crop.png")
    return crop


def cut(folder, crop, keep, saturate=1.0, joined=True):
    alpha = Image.new("L", SIZE, 0)
    draw = ImageDraw.Draw(alpha)
    for box in keep:
        draw.ellipse([box[0] - 10, box[1] - 10, box[2] + 10, box[3] + 10], fill=255)
    if joined and LEAF in keep:
        draw.line([(400, 300), (480, 380)], fill=255, width=30)
    rgba = crop.copy()
    rgba.putalpha(alpha)
    rgba.save(folder / "cut.png")
    final = rgba.crop(alpha.getbbox())
    if saturate != 1.0:
        from PIL import ImageEnhance

        rgb = ImageEnhance.Color(final.convert("RGB")).enhance(saturate)
        rgb.putalpha(final.getchannel("A"))
        final = rgb
    final.save(folder / "final.png")
    (folder / "params.json").write_text(json.dumps({"seeds": [[300, 240]]}))


def verdict(folder):
    return json.loads((folder / "qa.json").read_text())


def test_whole_vignette_passes(art_python, tmp_path):
    crop = plate(tmp_path)
    cut(tmp_path, crop, [BIRD, LEAF])
    result = art_python(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert verdict(tmp_path)["ok"]
    assert (tmp_path / "lost.jpg").exists() and (tmp_path / "disclosure.md").exists()


def test_eaten_pale_leaf_fails(art_python, tmp_path):
    crop = plate(tmp_path)
    cut(tmp_path, crop, [BIRD])
    result = art_python(tmp_path)
    assert result.returncode == 1
    lost = verdict(tmp_path)["lost"]
    assert not lost["ok"] and lost["pale"] + lost["strong"]


def test_dropped_region_is_not_a_loss(art_python, tmp_path):
    crop = plate(tmp_path)
    cut(tmp_path, crop, [BIRD])
    x0, y0, x1, y1 = LEAF
    x0, y0 = 392, 298  # the leaf and its whole stalk, clear of the bird
    drop = [[x0, y0], [x1 + 15, y0], [x1 + 15, y1 + 15], [x0, y1 + 15]]
    (tmp_path / "params.json").write_text(json.dumps({"seeds": [[300, 240]], "drop": [drop]}))
    assert art_python(tmp_path).returncode == 0
    assert "Removed 1" in (tmp_path / "disclosure.md").read_text()


def test_clipped_box_fails(art_python, tmp_path):
    clipped = [-60, 150, 160, 330]  # the bird runs off the left edge of the crop
    crop = plate(tmp_path, bird=clipped, leaf=False)
    cut(tmp_path, crop, [clipped])
    assert art_python(tmp_path).returncode == 1
    assert not verdict(tmp_path)["box"]["ok"]


def test_oversaturated_final_fails(art_python, tmp_path):
    crop = plate(tmp_path)
    cut(tmp_path, crop, [BIRD, LEAF], saturate=1.6)
    assert art_python(tmp_path).returncode == 1
    assert not verdict(tmp_path)["colour"]["ok"]


def test_foxing_far_from_the_drawing_is_reported_not_failed(art_python, tmp_path):
    crop = plate(tmp_path, leaf=False)
    draw = ImageDraw.Draw(crop)
    for x, y in [(60, 60), (540, 80), (80, 440), (560, 460), (300, 470)]:
        draw.ellipse([x, y, x + 9, y + 9], fill=(196, 150, 110))  # rust-coloured age spots
    crop.save(tmp_path / "crop.png")
    cut(tmp_path, crop, [BIRD])
    result = art_python(tmp_path)
    assert result.returncode == 0, result.stdout
    lost = verdict(tmp_path)["lost"]
    assert lost["specks"] and not lost["strong"] and not lost["pale"]


def test_small_loss_against_the_outline_fails(art_python, tmp_path):
    # a tail tip clipped off at the outline: small, but touching the kept drawing
    crop = plate(tmp_path, leaf=False)
    ImageDraw.Draw(crop).rectangle([420, 236, 446, 246], fill=(90, 70, 50))
    crop.save(tmp_path / "crop.png")
    cut(tmp_path, crop, [BIRD])
    assert art_python(tmp_path).returncode == 1
    assert not verdict(tmp_path)["lost"]["ok"]


def test_open_pocket_inside_the_outline_fails(art_python, tmp_path):
    # a pocket of page enclosed by the drawing but cut open (PR #322: page-tone it instead)
    crop = plate(tmp_path, leaf=False)
    cut(tmp_path, crop, [BIRD])
    for name in ("cut.png", "final.png"):
        image = Image.open(tmp_path / name)
        alpha = image.getchannel("A")
        x0, y0 = (0, 0) if name == "final.png" else (BIRD[0] - 10, BIRD[1] - 10)
        ImageDraw.Draw(alpha).rectangle([x0 + 90, y0 + 80, x0 + 130, y0 + 110], fill=0)
        image.putalpha(alpha)
        image.save(tmp_path / name)
    assert art_python(tmp_path).returncode == 1
    assert not verdict(tmp_path)["pockets"]["ok"]


def test_floating_part_fails(art_python, tmp_path):
    # a leaf kept as its own island, detached from the vignette (maintainer, #124)
    far = [470, 20, 560, 80]
    crop = plate(tmp_path, leaf=False)
    ImageDraw.Draw(crop).ellipse(far, fill=(120, 140, 90))
    crop.save(tmp_path / "crop.png")
    cut(tmp_path, crop, [BIRD, far], joined=False)
    assert art_python(tmp_path).returncode == 1
    assert not verdict(tmp_path)["floating"]["ok"]


def test_drawing_painted_over_in_page_tone_fails(art_python, tmp_path):
    # PR #348: a pale twig end kept inside the outline but faded to the page colour.
    # Nothing turned transparent, so the lost check passes; the erased check must not.
    crop = plate(tmp_path, leaf=False)
    twig = [(415, 240), (540, 240)]
    ImageDraw.Draw(crop).line(twig, fill=(212, 202, 182), width=10)
    crop.save(tmp_path / "crop.png")
    cut(tmp_path, crop, [BIRD, [415, 225, 545, 255]])
    painted = Image.open(tmp_path / "cut.png")
    ImageDraw.Draw(painted).line([(440, 240), (540, 240)], fill=PAGE + (255,), width=14)
    painted.save(tmp_path / "cut.png")
    result = art_python(tmp_path)
    assert result.returncode == 1, result.stdout
    qa = verdict(tmp_path)
    assert qa["lost"]["ok"] and not qa["erased"]["ok"]
    assert (tmp_path / "erased.jpg").exists()
