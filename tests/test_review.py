import json

import pytest
from conftest import cli
from test_boxing import BOX, GEOMETRY, KEY, imported, save
from test_imports import PLATE, reviewable

from fugart import boxing, review, store

# Fixtures are shared with the existing native import/box recovery tests.
assert imported and reviewable


@pytest.fixture
def boxed(imported, monkeypatch):
    workspace, root = imported
    monkeypatch.setattr(boxing, "launch", lambda *_: save(root) or {"returncode": 0})
    boxing.run(workspace, "Welcome Swallow")

    def native(root, workspace, script, *args):
        from pathlib import Path

        if script.endswith("build.py"):
            folder = Path(args[0])
            (folder / "birds.js").write_text("window.BIRDS = [];")
            (folder / "index.html").write_text("review")
            # The real build.py writes four aligned layers per plate.
            for plate in json.loads((folder / "plates.json").read_text()):
                layers = folder / "img" / plate["id"]
                layers.mkdir(parents=True)
                for name in ("cut.png", "plate.png", "scan.png", "flags.jpg"):
                    (layers / name).write_bytes(b"layer")
        else:
            folder = Path(args[0])
            folder.mkdir()
            for name in ("web.png", "panel.png"):
                (folder / name).write_bytes(b"preview")
        return {"returncode": 0, "stdout": "built", "stderr": ""}

    monkeypatch.setattr(review.upstream, "native", native)
    return workspace, root


def test_build_is_not_approval_and_approve_names_exact_review(boxed):
    workspace, _ = boxed
    made = review.build(workspace, "Welcome Swallow")
    assert made["state"] == "complete"
    with store.connect(workspace) as db:
        assert store.find_species(db, "Welcome Swallow")["state"] == "boxed"
        assert review.show(db, workspace, "Welcome Swallow")["valid"]
    result = review.approve(workspace, "Welcome Swallow", made["id"], "Reviewer", "Clean")
    assert result["phase"] == "approved"
    shown = cli(workspace, "show", "Welcome Swallow", "--json")
    assert shown["review"]["approval"]["reviewer"] == "Reviewer"
    assert shown["state"] == "approved"


@pytest.mark.parametrize("damage", ["plate", "geometry", "preview", "source", "evidence", "scan"])
def test_changed_evidence_invalidates_approval_and_display(boxed, damage):
    workspace, root = boxed
    made = review.build(workspace, "Welcome Swallow")
    review.approve(workspace, "Welcome Swallow", made["id"], "Reviewer", "Clean")
    if damage == "plate":
        (root / PLATE).write_bytes(b"edited")
    elif damage == "geometry":
        (root / GEOMETRY).write_text(json.dumps({KEY: BOX | {"box": [0, 0, 1, 1]}}))
    elif damage == "preview":
        (workspace / made["record"]["folder"] / "preview/web.png").write_bytes(b"edited")
    elif damage == "source":
        with store.connect(workspace) as db:
            db.execute("UPDATE sources SET fingerprint='edited'")
    elif damage == "evidence":
        next((workspace / "work/evidence").glob("*.bin")).write_bytes(b"edited evidence")
    else:
        next(p for p in (workspace / "work/scans").iterdir() if p.is_file()).write_bytes(
            b"edited scan"
        )
    shown = cli(workspace, "show", "Welcome Swallow", "--json")
    assert not shown["review"]["valid"]
    assert shown["state"] == "boxed"
    with pytest.raises(ValueError, match="stale"):
        review.approve(workspace, "Welcome Swallow", made["id"], "Reviewer", "Again")


def test_failure_keeps_bird_boxed_and_cannot_be_approved(boxed, monkeypatch):
    workspace, _ = boxed
    monkeypatch.setattr(review.upstream, "native", lambda *_: {"returncode": 1, "stderr": "broken"})
    with pytest.raises(ValueError, match="broken"):
        review.build(workspace, "Welcome Swallow")
    with store.connect(workspace) as db:
        shown = review.show(db, workspace, "Welcome Swallow")
        assert shown["state"] == "failed"
    with pytest.raises(ValueError, match="complete"):
        review.approve(workspace, "Welcome Swallow", shown["id"], "Reviewer", "Clean")


def test_approval_requires_identity_notes_and_explicit_cli_gate(boxed):
    workspace, _ = boxed
    made = review.build(workspace, "Welcome Swallow")
    with pytest.raises(ValueError, match="reviewer"):
        review.approve(workspace, "Welcome Swallow", made["id"], "", "Clean")
    assert "confirm-quality" in cli(
        workspace,
        "review",
        "approve",
        "Welcome Swallow",
        "--review",
        str(made["id"]),
        "--reviewer",
        "Reviewer",
        "--notes",
        "Clean",
        ok=False,
    )


def test_new_review_requires_new_approval(boxed):
    workspace, _ = boxed
    first = review.build(workspace, "Welcome Swallow")
    review.approve(workspace, "Welcome Swallow", first["id"], "Reviewer", "Clean")
    second = review.build(workspace, "Welcome Swallow")
    with store.connect(workspace) as db:
        assert not review.show(db, workspace, "Welcome Swallow")["approved"]
        assert store.show_species(db, "Welcome Swallow")["state"] == "boxed"
    with pytest.raises(ValueError, match="latest"):
        review.approve(workspace, "Welcome Swallow", first["id"], "Reviewer", "Clean")
    assert (
        review.approve(workspace, "Welcome Swallow", second["id"], "Reviewer", "Clean")["phase"]
        == "approved"
    )


def test_inputs_changed_during_build_cannot_complete(boxed, monkeypatch):
    workspace, root = boxed
    native = review.upstream.native

    def changed(*args):
        result = native(*args)
        (root / PLATE).write_bytes(b"edited while rendering")
        return result

    monkeypatch.setattr(review.upstream, "native", changed)
    with pytest.raises(ValueError, match="changed"):
        review.build(workspace, "Welcome Swallow")
    with store.connect(workspace) as db:
        assert review.show(db, workspace, "Welcome Swallow")["state"] == "failed"


def test_stale_approval_is_not_listed_as_approved(boxed):
    workspace, root = boxed
    made = review.build(workspace, "Welcome Swallow")
    review.approve(workspace, "Welcome Swallow", made["id"], "Reviewer", "Clean")
    (root / PLATE).write_bytes(b"edited")
    assert cli(workspace, "list", "--state", "approved", "--json") == []
    assert cli(workspace, "status", "--json")["states"]["boxed"] == 1
    with store.connect(workspace) as db:
        db.execute("INSERT INTO batches VALUES ('pilot-01', 'fixture', '2026-10-11')")
        db.execute("INSERT INTO batch_members VALUES ('pilot-01', 'hirundo-neoxena')")
    assert cli(workspace, "batch", "show", "pilot-01", "--json")["members"][0]["state"] == "boxed"
