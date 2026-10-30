import json

import pytest
from conftest import cli
from test_cutout import phase
from test_imports import MANIFEST, PLATE, bring_in

from fugart import boxing, store

pytest_plugins = ["test_imports"]

GEOMETRY = "assets/artwork/classic/geometry.json"
KEY = "birds/hirundo-neoxena.webp"
BOX = {"box": [0.1, 0.2, 0.8, 0.9], "cut": [800, 700]}


@pytest.fixture
def imported(reviewable, monkeypatch):
    workspace, root = reviewable
    bring_in(workspace)
    monkeypatch.setattr(boxing, "measure", lambda *_: [800, 700])
    return workspace, root


def save(root, value=BOX, **extra):
    (root / GEOMETRY).write_text(json.dumps({KEY: value} | extra))


def journal(workspace):
    with store.connect(workspace, readonly=True) as db:
        row = db.execute("SELECT * FROM operations WHERE kind='box' ORDER BY id DESC").fetchone()
        return dict(row) | {"record": json.loads(row["record_json"])}


def test_box_journals_before_launch_and_records_saved_geometry(imported, monkeypatch):
    workspace, root = imported

    def launch(*args):
        assert journal(workspace)["state"] == "started"
        assert phase(workspace) == "imported"
        save(root)
        return {"returncode": 0}

    monkeypatch.setattr(boxing, "launch", launch)
    result = boxing.run(workspace, "Welcome Swallow")
    assert result["phase"] == phase(workspace) == "boxed"
    row = journal(workspace)
    assert row["state"] == "complete"
    assert row["record"]["geometry_entry"] == BOX
    assert row["record"]["plate"] == PLATE
    assert row["record"]["upstream_revision"]
    assert cli(workspace, "show", "Welcome Swallow", "--json")["box"]["state"] == "complete"


def test_no_edit_leaves_imported_and_allows_a_new_session(imported, monkeypatch):
    workspace, root = imported
    monkeypatch.setattr(boxing, "launch", lambda *_: {"returncode": 0})
    with pytest.raises(ValueError, match="no box was saved"):
        boxing.run(workspace, "Welcome Swallow")
    assert phase(workspace) == "imported" and journal(workspace)["state"] == "failed"
    monkeypatch.setattr(boxing, "launch", lambda *_: save(root) or {"returncode": 0})
    assert boxing.run(workspace, "Welcome Swallow")["phase"] == "boxed"


def test_crash_after_save_reconciles_without_relaunch(imported, monkeypatch):
    workspace, root = imported

    def interrupted(*_):
        save(root)
        raise RuntimeError("process killed after autosave")

    monkeypatch.setattr(boxing, "launch", interrupted)
    with pytest.raises(RuntimeError):
        boxing.run(workspace, "Welcome Swallow")
    assert journal(workspace)["state"] == "started" and phase(workspace) == "imported"
    monkeypatch.setattr(boxing, "launch", lambda *_: pytest.fail("must reconcile first"))
    assert boxing.run(workspace, "Welcome Swallow")["phase"] == "boxed"


@pytest.mark.parametrize(
    "damage", ["plate", "manifest", "unrelated", "stale", "malformed", "other-box"]
)
def test_conflicts_stay_pending_without_relaunch(imported, monkeypatch, damage):
    workspace, root = imported

    def launch(*_):
        save(root)
        if damage == "plate":
            (root / PLATE).write_bytes(b"changed image")
        elif damage == "manifest":
            (root / MANIFEST).write_text("{}")
        elif damage == "unrelated":
            (root / "stray.txt").write_text("unexpected")
        elif damage == "stale":
            save(root, BOX | {"cut": [799, 700]})
        elif damage == "malformed":
            save(root, BOX | {"box": [0.9, 0, 0.1, 1]})
        else:
            save(root, **{"birds/other.webp": BOX})
        return {"returncode": 0}

    monkeypatch.setattr(boxing, "launch", launch)
    with pytest.raises(ValueError, match="repair"):
        boxing.run(workspace, "Welcome Swallow")
    assert journal(workspace)["state"] == "started" and phase(workspace) == "imported"
    monkeypatch.setattr(boxing, "launch", lambda *_: pytest.fail("must reconcile first"))
    with pytest.raises(ValueError, match="repair"):
        boxing.run(workspace, "Welcome Swallow")


def test_preflight_refuses_altered_import_or_unrelated_changes(imported, monkeypatch):
    workspace, root = imported
    monkeypatch.setattr(boxing, "launch", lambda *_: pytest.fail("preflight must refuse"))
    (root / "stray.txt").write_text("user work")
    with pytest.raises(ValueError, match="uncommitted"):
        boxing.run(workspace, "Welcome Swallow")
    (root / "stray.txt").unlink()
    (root / PLATE).write_bytes(b"edited plate")
    with pytest.raises(ValueError, match="import"):
        boxing.run(workspace, "Welcome Swallow")


def test_box_requires_active_unblocked_imported_bird(imported):
    workspace, _ = imported
    with store.connect(workspace) as db:
        db.execute(
            "UPDATE species SET blocked_reason='artwork-quality' WHERE key='hirundo-neoxena'"
        )
    with pytest.raises(ValueError, match="unblocked"):
        boxing.run(workspace, "Welcome Swallow")


def test_cli_runs_native_editor_with_browser_suppressed(reviewable):
    workspace, _ = reviewable
    bring_in(workspace)
    result = cli(workspace, "box", "Welcome Swallow", "--port", "8123", "--json")
    assert result["phase"] == "boxed"
    record = journal(workspace)["record"]
    assert record["commands"][0]["BROWSER"] == "true"
    assert record["commands"][0]["stdout"].strip() == "saved"
    assert record["commands"][0]["argv"][5:] == ["tools/bird_box.py", PLATE, "--port", "8123"]


def test_moved_head_during_boxing_stops_for_repair(imported, monkeypatch):
    import subprocess

    workspace, root = imported

    def launch(*_):
        save(root)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.org",
                "commit",
                "--allow-empty",
                "-qm",
                "advance fixture",
            ],
            check=True,
        )
        return {"returncode": 0}

    monkeypatch.setattr(boxing, "launch", launch)
    with pytest.raises(ValueError, match="HEAD moved"):
        boxing.run(workspace, "Welcome Swallow")
    assert journal(workspace)["state"] == "started" and phase(workspace) == "imported"


def test_agent_box_is_explicitly_proposed_and_still_needs_quality_signoff(imported):
    workspace, root = imported
    result = boxing.propose(
        workspace,
        "Welcome Swallow",
        [0.1, 0.2, 0.8, 0.9],
        "Primary bird includes bill, feet and tail",
    )
    assert result["phase"] == "boxed"
    assert json.loads((root / GEOMETRY).read_text())[KEY] == BOX
    # upstream's own writers (bird_box.py, add_bird.py) use indent=1; anything else rewrites
    # every line of geometry.json in the PR diff
    assert (root / GEOMETRY).read_text() == json.dumps({KEY: BOX}, indent=1, sort_keys=True) + "\n"
    assert journal(workspace)["record"]["box_method"] == "agent-proposed; human sign-off pending"
    assert phase(workspace) == "boxed"


@pytest.mark.parametrize("values", [[1, 0, 0, 1], [0, 0, float("nan"), 1], [0, 0, 2, 1]])
def test_agent_box_rejects_invalid_coordinates_before_writes(imported, values):
    workspace, root = imported
    with pytest.raises(ValueError, match="box"):
        boxing.propose(workspace, "Welcome Swallow", values, "check")
    assert not (root / GEOMETRY).exists()
