import json
import shutil

import pytest
from PIL import Image
from test_boxing import GEOMETRY, imported
from test_imports import PLATE, reviewable
from test_review import boxed

from fugart import boxing, cutout, imports, review, revisions, store

assert imported and reviewable and boxed


@pytest.fixture
def candidate(boxed, tmp_path, monkeypatch):
    workspace, root = boxed
    folder = tmp_path / "candidate"
    folder.mkdir()
    old = workspace / "work/cuts/hirundo-neoxena/r001"
    shutil.copyfile(old / "crop.png", folder / "crop.png")
    with Image.open(old / "cut.png") as im:
        im.convert("RGBA").save(folder / "cut.png")
    with Image.open(old / "final.png") as im:
        im.convert("RGBA").save(folder / "final.png")

    # Native staging boundary only: the fixture's native tools copy instead of rendering.
    def prepare(root, workspace, folder):
        shutil.copyfile(folder / "final.png", folder / "shipped.webp")
        shutil.copyfile(folder / "final.png", folder / "preview.png")
        return {"returncode": 0, "stdout": "prepared", "stderr": ""}

    monkeypatch.setattr(revisions, "prepare", prepare)
    return workspace, root, folder


def replace(candidate):
    workspace, _, folder = candidate
    return revisions.replace(workspace, "Welcome Swallow", folder, "Compact pair selected")


def test_replace_preserves_history_and_invalidates_old_review(candidate):
    workspace, root, folder = candidate
    previous = review.build(workspace, "Welcome Swallow")
    review.approve(workspace, "Welcome Swallow", previous["id"], "Reviewer", "Old cut clean")
    before = (root / PLATE).read_bytes()
    result = replace(candidate)
    assert result["phase"] == "boxed"
    assert (root / PLATE).read_bytes() == (folder / "final.png").read_bytes()
    assert sorted((root / PLATE).parent.glob("hirundo-neoxena*")) == [root / PLATE]
    with store.connect(workspace) as db:
        cuts = cutout.show(db, workspace, "Welcome Swallow")
        assert cuts["revisions"] == [1, 2] and cuts["current"]["intact"]
        assert (workspace / cuts["current"]["path"] / "previous.webp").read_bytes() == before
        assert imports.show(db, "Welcome Swallow")["kind"] == "replace"
        imports.accounted_changes(db, root)
        assert not review.show(db, workspace, "Welcome Swallow")["valid"]
        assert not review.show(db, workspace, "Welcome Swallow")["approved"]
    fresh = review.build(workspace, "Welcome Swallow")
    assert fresh["record"]["inputs"]["cut_revision"] == 2
    assert replace(candidate)["operation"] == result["operation"]


@pytest.mark.parametrize("damage", ["crop", "unrelated", "source"])
def test_replace_rejects_invalid_preimage_without_writes(candidate, damage):
    workspace, root, folder = candidate
    before = (root / PLATE).read_bytes()
    if damage == "crop":
        (folder / "crop.png").write_bytes(b"wrong scan crop")
    elif damage == "unrelated":
        (root / "user.txt").write_text("keep")
    else:
        with store.connect(workspace) as db:
            db.execute("UPDATE sources SET fingerprint='changed'")
    with pytest.raises((ValueError, OSError)):
        replace(candidate)
    assert (root / PLATE).read_bytes() == before
    with store.connect(workspace) as db:
        assert cutout.show(db, workspace, "Welcome Swallow")["revisions"] == [1]


def test_replace_recovers_after_first_write_without_another_revision(candidate, monkeypatch):
    workspace, root, _ = candidate
    write = revisions.atomic_write

    def interrupted(path, data):
        write(path, data)
        if str(path).endswith(".webp"):
            raise RuntimeError("interrupted after plate write")

    monkeypatch.setattr(revisions, "atomic_write", interrupted)
    with pytest.raises(RuntimeError):
        replace(candidate)
    with store.connect(workspace) as db:
        assert (
            db.execute("SELECT state FROM operations WHERE kind='replace'").fetchone()[0]
            == "started"
        )
    monkeypatch.setattr(revisions, "atomic_write", write)
    assert replace(candidate)["phase"] == "boxed"
    with store.connect(workspace) as db:
        imports.accounted_changes(db, root)
        assert cutout.show(db, workspace, "Welcome Swallow")["revisions"] == [1, 2]
        assert len(list(db.execute("SELECT id FROM operations WHERE kind='replace'"))) == 1


def test_replace_does_not_overwrite_conflicting_geometry_on_resume(candidate, monkeypatch):
    _workspace, root, _ = candidate
    write = revisions.atomic_write

    def interrupted(path, data):
        write(path, data)
        raise RuntimeError("interrupted")

    monkeypatch.setattr(revisions, "atomic_write", interrupted)
    with pytest.raises(RuntimeError):
        replace(candidate)
    (root / GEOMETRY).write_text(json.dumps({"user-edit": "keep"}))
    monkeypatch.setattr(revisions, "atomic_write", write)
    with pytest.raises(ValueError, match="geometry"):
        replace(candidate)
    assert json.loads((root / GEOMETRY).read_text()) == {"user-edit": "keep"}


def test_box_projection_uses_original_crop_coordinates(tmp_path):
    from PIL import ImageDraw

    old = Image.new("RGBA", (100, 100))
    ImageDraw.Draw(old).rectangle((10, 10, 89, 89), fill=(0, 0, 0, 255))
    new = Image.new("RGBA", (100, 100))
    ImageDraw.Draw(new).rectangle((20, 20, 79, 79), fill=(0, 0, 0, 255))
    old.save(tmp_path / "old.png")
    new.save(tmp_path / "new.png")
    box = revisions.project_box(
        tmp_path / "old.png", tmp_path / "new.png", [0.25, 0.25, 0.75, 0.75]
    )
    assert box == pytest.approx([1 / 6, 1 / 6, 5 / 6, 5 / 6])


def test_resume_refuses_tampered_staged_plate(candidate, monkeypatch):
    workspace, _root, _ = candidate

    def interrupted(*_):
        raise RuntimeError("interrupted before first write")

    monkeypatch.setattr(revisions, "atomic_write", interrupted)
    with pytest.raises(RuntimeError):
        replace(candidate)
    (workspace / "work/cuts/hirundo-neoxena/r002/shipped.webp").chmod(0o644)
    (workspace / "work/cuts/hirundo-neoxena/r002/shipped.webp").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="cut changed"):
        replace(candidate)


def test_identical_crop_pixels_with_different_png_encoding_are_accepted(candidate):
    workspace, _root, folder = candidate
    old = workspace / "work/cuts/hirundo-neoxena/r001/crop.png"
    with Image.open(folder / "crop.png") as image:
        image.save(folder / "crop.png", compress_level=0)
    assert old.read_bytes() != (folder / "crop.png").read_bytes()
    assert replace(candidate)["phase"] == "boxed"


def test_box_projection_follows_a_wider_crop_of_the_same_scan(tmp_path):
    from PIL import ImageDraw

    old = Image.new("RGBA", (100, 100))
    ImageDraw.Draw(old).rectangle((10, 10, 89, 89), fill=(0, 0, 0, 255))
    new = Image.new("RGBA", (200, 200))
    ImageDraw.Draw(new).rectangle((20, 20, 179, 179), fill=(0, 0, 0, 255))
    old.save(tmp_path / "old.png")
    new.save(tmp_path / "new.png")
    # the old crop is scan px 100-200 at full size; the new one is the whole 400 px scan halved
    box = revisions.project_box(
        tmp_path / "old.png",
        tmp_path / "new.png",
        [0.25, 0.25, 0.75, 0.75],
        frames=([100, 100, 200, 200], [0, 0, 400, 400]),
    )
    assert box == pytest.approx([0.28125, 0.28125, 0.40625, 0.40625])


def recropped(candidate, monkeypatch, native=(255, 250, 240)):
    """A candidate cut from a wider box; `native` is what plate.py crop makes of its spec."""
    workspace, _root, folder = candidate
    old = workspace / "work/cuts/hirundo-neoxena/r001"
    spec = json.loads((old / "spec.json").read_text())
    (folder / "spec.json").write_text(json.dumps(spec | {"box": [0, 0, 1600, 1400]}))
    Image.new("RGB", (400, 350), (255, 250, 240)).save(folder / "crop.png")
    for name in ("cut.png", "final.png"):
        Image.new("RGBA", (400, 350), (0, 0, 0, 255)).save(folder / name)

    def crop(root, workspace, spec_path):
        assert json.loads(spec_path.read_text())["box"] == [0, 0, 1600, 1400]
        Image.new("RGB", (400, 350), native).save(spec_path.parent / "crop.png")
        return {"returncode": 0, "stdout": "crop (400, 350)", "stderr": ""}

    monkeypatch.setattr(revisions, "native_crop", crop)


def test_replace_accepts_a_wider_crop_the_native_tool_reproduces(candidate, monkeypatch):
    workspace, _root, _folder = candidate
    recropped(candidate, monkeypatch)
    result = replace(candidate)
    assert result["phase"] == "boxed"
    # twice the box at a quarter of the scale: the old box keeps its scan pixels
    assert result["geometry_entry"]["box"] == pytest.approx([0.05, 0.1, 0.4, 0.45])
    with store.connect(workspace) as db:
        current = cutout.show(db, workspace, "Welcome Swallow")["current"]
    made = workspace / current["path"]
    assert json.loads((made / "spec.json").read_text())["box"] == [0, 0, 1600, 1400]
    with Image.open(made / "crop.png") as crop:
        assert crop.size == (400, 350)
    assert any("re-cropped" in line for line in current["disclosures"])
    assert review.build(workspace, "Welcome Swallow")["record"]["inputs"]["cut_revision"] == 2


def test_replace_rejects_a_crop_the_native_tool_does_not_reproduce(candidate, monkeypatch):
    workspace, root, _ = candidate
    before = (root / PLATE).read_bytes()
    recropped(candidate, monkeypatch, native=(0, 0, 0))
    with pytest.raises(ValueError, match="native crop"):
        replace(candidate)
    assert (root / PLATE).read_bytes() == before
    with store.connect(workspace) as db:
        assert cutout.show(db, workspace, "Welcome Swallow")["revisions"] == [1]


def test_replace_rejects_a_different_crop_without_a_spec(candidate, monkeypatch):
    _workspace, _root, folder = candidate
    recropped(candidate, monkeypatch)
    (folder / "spec.json").unlink()
    with pytest.raises(ValueError, match="original approved scan crop"):
        replace(candidate)


def test_replaced_bird_can_be_boxed_again_and_needs_fresh_review(candidate, monkeypatch):
    workspace, root, _ = candidate
    replace(candidate)
    monkeypatch.setattr(boxing, "measure", lambda *_: imports_cut(workspace))
    result = boxing.propose(workspace, "Welcome Swallow", [0.2, 0.3, 0.7, 0.6], "Bird only")
    assert result["phase"] == "boxed"
    geometry = json.loads((root / GEOMETRY).read_text())
    assert geometry["birds/hirundo-neoxena.webp"]["box"] == [0.2, 0.3, 0.7, 0.6]
    built = review.build(workspace, "Welcome Swallow")
    assert built["record"]["inputs"]["geometry_entry"]["box"] == [0.2, 0.3, 0.7, 0.6]


def test_rebox_refuses_a_box_edited_outside_the_journal(candidate, monkeypatch):
    workspace, root, _ = candidate
    replace(candidate)
    monkeypatch.setattr(boxing, "measure", lambda *_: imports_cut(workspace))
    geometry = json.loads((root / GEOMETRY).read_text())
    geometry["birds/hirundo-neoxena.webp"]["box"] = [0.1, 0.1, 0.5, 0.5]
    (root / GEOMETRY).write_text(json.dumps(geometry))
    with pytest.raises(ValueError):
        boxing.propose(workspace, "Welcome Swallow", [0.2, 0.3, 0.7, 0.6], "Bird only")


def imports_cut(workspace):
    with store.connect(workspace, readonly=True) as db:
        return imports.show(db, "Welcome Swallow")["record"]["cut"]
