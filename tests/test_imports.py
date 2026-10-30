import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from conftest import cli
from test_cutout import PLATE_URL, cutout, phase, upstream_status, write_spec
from test_fetch import Response, png
from test_sources import approve, record

from fugart import fetch, imports, store, upstream

PLATE = "assets/artwork/classic/birds/hirundo-neoxena.webp"
MANIFEST = "assets/artwork/classic/manifest.json"
ENTRY = {"birds/hirundo-neoxena.webp": {"source": "gould", "url": PLATE_URL}}


@pytest.fixture
def reviewable(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(png()))
    scan = workspace / fetch.acquire(workspace, "Welcome Swallow")["path"]
    cutout(workspace, "--spec", str(write_spec(workspace, scan)))
    with store.connect(workspace, readonly=True) as db:
        return workspace, Path(store.config(db)["upstream_path"])


def bring_in(workspace, ok=True):
    return cli(workspace, "import", "Welcome Swallow", "--json", ok=ok)


def journal(workspace):
    shown = cli(workspace, "show", "Welcome Swallow", "--json")
    return shown["import"], shown["events"][-1]["action"]


def plates(root):
    return sorted(path.name for path in (root / PLATE).parent.glob("hirundo-neoxena*"))


def test_import_writes_one_plate_and_its_manifest_entry(reviewable):
    workspace, root = reviewable
    final = workspace / "work/cuts/hirundo-neoxena/r001/final.png"
    result = bring_in(workspace)
    assert (result["phase"], result["plate"]) == ("imported", PLATE)
    assert (root / PLATE).read_bytes() == final.read_bytes()
    assert json.loads((root / MANIFEST).read_text()) == ENTRY
    assert upstream_status(workspace).splitlines() == [f" M {MANIFEST}", f"?? {PLATE}"]
    assert phase(workspace) == "imported"
    operation, last = journal(workspace)
    assert (operation["state"], last) == ("complete", "imported")
    made = operation["record"]
    assert made["commands"][0]["argv"][5:] == [
        "tools/add_bird.py",
        str(final),
        "--style",
        "classic",
        "--key",
        "hirundo-neoxena",
        "--source",
        "gould",
        "--url",
        PLATE_URL,
        "--no-detect",
        "--no-box",
    ]
    assert (made["cut_revision"], made["source_revision"], made["size"]) == (1, 1, [800, 700])
    assert made["plate_sha256"] == result["plate_sha256"] and len(made["upstream_revision"]) == 40


def test_a_second_import_is_refused_without_a_second_variant(reviewable):
    workspace, root = reviewable
    bring_in(workspace)
    assert "needs-review" in bring_in(workspace, ok=False)
    assert plates(root) == ["hirundo-neoxena.webp"]
    assert json.loads((root / MANIFEST).read_text()) == ENTRY


def test_an_import_that_wrote_nothing_can_be_retried(reviewable, monkeypatch):
    workspace, root = reviewable
    monkeypatch.setenv("FIXTURE_FAIL", "import")
    assert "fixture import failed" in bring_in(workspace, ok=False)
    assert phase(workspace) == "needs-review" and upstream_status(workspace) == ""
    operation, last = journal(workspace)
    assert (operation["state"], last) == ("failed", "import-failed")
    monkeypatch.delenv("FIXTURE_FAIL")
    assert bring_in(workspace)["phase"] == "imported"
    assert plates(root) == ["hirundo-neoxena.webp"]


def test_a_partial_import_stops_for_repair_and_is_never_run_again(reviewable, monkeypatch):
    workspace, root = reviewable
    monkeypatch.setenv("FIXTURE_FAIL", "manifest")
    assert "repair" in bring_in(workspace, ok=False)
    monkeypatch.delenv("FIXTURE_FAIL")
    for _ in range(2):
        assert "repair" in bring_in(workspace, ok=False)
    # nothing deleted, no "-2" variant made by a blind retry, and the bird has not advanced
    assert plates(root) == ["hirundo-neoxena.webp"]
    assert json.loads((root / MANIFEST).read_text()) == {}
    assert phase(workspace) == "needs-review"
    operation, last = journal(workspace)
    assert (operation["state"], last) == ("started", "import-conflict")
    (root / MANIFEST).write_text(json.dumps(ENTRY))
    assert bring_in(workspace)["phase"] == "imported"
    operation, _ = journal(workspace)
    assert operation["state"] == "complete" and len(operation["record"]["commands"]) == 1
    assert plates(root) == ["hirundo-neoxena.webp"]


def test_a_tool_that_dies_after_writing_everything_still_counts_once(reviewable, monkeypatch):
    workspace, root = reviewable
    monkeypatch.setenv("FIXTURE_FAIL", "after")
    assert bring_in(workspace)["phase"] == "imported"
    operation, _ = journal(workspace)
    assert operation["record"]["commands"][0]["returncode"] != 0
    assert plates(root) == ["hirundo-neoxena.webp"]


def test_an_import_killed_after_the_writes_is_reconciled_from_the_journal(reviewable):
    workspace, root = reviewable
    # this process "dies" between add_bird finishing and the result being recorded
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        _, planned = imports.begin(db, workspace, store.find_species(db, "Welcome Swallow"))
    assert upstream.native(root, workspace, *planned["args"])["returncode"] == 0
    assert phase(workspace) == "needs-review"
    assert bring_in(workspace)["phase"] == "imported"
    operation, _ = journal(workspace)
    assert (operation["state"], operation["record"]["commands"]) == ("complete", [])
    assert plates(root) == ["hirundo-neoxena.webp"]
    assert json.loads((root / MANIFEST).read_text()) == ENTRY


def test_import_refuses_a_checkout_with_other_changes(reviewable):
    workspace, root = reviewable
    (root / "notes.txt").write_text("someone else's work in progress")
    assert "uncommitted" in bring_in(workspace, ok=False)
    assert plates(root) == [] and journal(workspace)[0] is None


def test_import_refuses_an_upstream_that_moved_since_the_dry_run(reviewable):
    workspace, root = reviewable
    git = ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "commit", "-qm", "upstream moved", "--allow-empty"], check=True)
    assert "recut" in bring_in(workspace, ok=False)
    assert plates(root) == []


def test_import_needs_an_intact_cut_awaiting_review(reviewable):
    workspace, root = reviewable
    cli(workspace, "block", "Welcome Swallow", "--reason", "artwork-quality", "--notes", "Hold")
    assert "unblocked" in bring_in(workspace, ok=False)
    cli(workspace, "unblock", "Welcome Swallow", "--notes", "Resolved")
    final = workspace / "work/cuts/hirundo-neoxena/r001/final.png"
    final.chmod(0o644)
    final.write_bytes(png() + b"edited after review")
    assert "intact" in bring_in(workspace, ok=False)
    assert plates(root) == [] and phase(workspace) == "needs-review"


def test_schema_three_workspace_migrates_before_importing(reviewable):
    workspace, _ = reviewable
    with sqlite3.connect(workspace / ".fugart/state.sqlite3") as db:
        db.execute("DROP TABLE operations")
        db.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    assert "fugart migrate" in bring_in(workspace, ok=False)
    assert cli(workspace, "show", "Welcome Swallow", "--json")["import"] is None
    result = cli(workspace, "migrate", "--json")
    assert (result["schema_version"], result["changed"]) == (4, True)
    assert bring_in(workspace)["phase"] == "imported"


def completed_neighbor(workspace, root, *, boxed=False):
    plate = "assets/artwork/classic/birds/coloeus-monedula.webp"
    image = workspace / "work/cuts/hirundo-neoxena/r001/final.png"
    (root / plate).write_bytes(image.read_bytes())
    entry = {"source": "gould", "url": "https://example.org/jackdaw"}
    manifest = json.loads((root / MANIFEST).read_text())
    manifest["birds/coloeus-monedula.webp"] = entry
    (root / MANIFEST).write_text(json.dumps(manifest))
    made = {
        "plate": plate,
        "plate_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "manifest": MANIFEST,
        "manifest_key": "birds/coloeus-monedula.webp",
        "manifest_entry": entry,
    }
    with store.connect(workspace) as db:
        db.execute(
            "INSERT INTO operations (species_key, kind, state, record_json, created_at, updated_at) "
            "VALUES ('hirundo-neoxena', 'import', 'complete', ?, 'test', 'test')",
            (json.dumps(made),),
        )
        if boxed:
            geometry = "assets/artwork/classic/geometry.json"
            box = {"box": [0.1, 0.1, 0.9, 0.9], "cut": [800, 700]}
            (root / geometry).write_text(json.dumps({made["manifest_key"]: box}))
            db.execute(
                "INSERT INTO operations (species_key, kind, state, record_json, created_at, updated_at) "
                "VALUES ('hirundo-neoxena', 'box', 'complete', ?, 'test', 'test')",
                (
                    json.dumps(
                        {
                            "geometry": geometry,
                            "geometry_key": made["manifest_key"],
                            "geometry_entry": box,
                        }
                    ),
                ),
            )
    return plate


@pytest.mark.parametrize("boxed", [False, True])
def test_import_preserves_verified_completed_neighbor_changes(reviewable, boxed):
    workspace, root = reviewable
    neighbor = completed_neighbor(workspace, root, boxed=boxed)
    image_before = (root / neighbor).read_bytes()
    assert bring_in(workspace)["phase"] == "imported"
    assert (root / neighbor).read_bytes() == image_before
    assert set(json.loads((root / MANIFEST).read_text())) == {
        "birds/coloeus-monedula.webp",
        "birds/hirundo-neoxena.webp",
    }


@pytest.mark.parametrize("damage", ["plate", "manifest", "geometry", "unrelated"])
def test_completed_journal_never_excuses_unaccounted_content(reviewable, damage):
    workspace, root = reviewable
    neighbor = completed_neighbor(workspace, root, boxed=True)
    path = {
        "plate": neighbor,
        "manifest": MANIFEST,
        "geometry": "assets/artwork/classic/geometry.json",
        "unrelated": "stray.txt",
    }[damage]
    (root / path).write_text("{}")
    assert "uncommitted" in bring_in(workspace, ok=False)
    assert not (root / PLATE).exists()


def test_current_head_revalidation_reuses_cut_and_enables_import(reviewable):
    from fugart import cutout as cuts

    workspace, root = reviewable
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "move",
        ],
        check=True,
    )
    before = (workspace / "work/cuts/hirundo-neoxena/r001/log.json").read_bytes()
    made = cuts.revalidate(workspace, "Welcome Swallow")
    assert made["state"] == "complete"
    assert bring_in(workspace)["phase"] == "imported"
    assert (workspace / "work/cuts/hirundo-neoxena/r001/log.json").read_bytes() == before
    with store.connect(workspace) as db:
        assert cuts.show(db, workspace, "Welcome Swallow")["revisions"] == [1]


def test_revalidation_preview_tampering_refuses_import(reviewable):
    from fugart import cutout as cuts

    workspace, root = reviewable
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "move",
        ],
        check=True,
    )
    made = cuts.revalidate(workspace, "Welcome Swallow")
    (workspace / made["record"]["folder"] / "preview.png").write_bytes(b"changed")
    assert "revalidat" in bring_in(workspace, ok=False)
    assert plates(root) == []
