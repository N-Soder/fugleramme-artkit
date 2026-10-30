import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from conftest import cli
from PIL import Image
from test_fetch import Response, png
from test_sources import approve, record

from fugart import fetch, store

PLATE_URL = "https://archive.org/details/birdsAustraliav2Goul/page/n36"


@pytest.fixture
def downloaded(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(png()))
    return workspace, workspace / fetch.acquire(workspace, "Welcome Swallow")["path"]


def write_spec(workspace, scan, **extra):
    path = workspace / "work/swallow-spec.json"
    spec = {"scan": str(scan), "box": [0, 0, 800, 700], "paper_min": 232, "seed": [400, 350]}
    path.write_text(json.dumps(spec | extra))
    return path


def cutout(workspace, *args, ok=True):
    return cli(workspace, "cutout", "Welcome Swallow", *args, "--json", ok=ok)


def phase(workspace):
    return cli(workspace, "show", "Welcome Swallow", "--json")["state"]


def upstream_status(workspace):
    with store.connect(workspace, readonly=True) as db:
        root = store.config(db)["upstream_path"]
    return subprocess.run(
        ["git", "-C", root, "status", "--porcelain", "--ignored"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def digests(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}


def test_cutout_runs_native_tools_into_a_recorded_revision(downloaded):
    workspace, scan = downloaded
    original = scan.read_bytes()
    result = cutout(workspace, "--spec", str(write_spec(workspace, scan)), "--notes", "First cut")
    folder = workspace / "work/cuts/hirundo-neoxena/r001"
    assert (result["revision"], result["phase"]) == (1, "needs-review")
    assert result["path"] == "work/cuts/hirundo-neoxena/r001"
    assert phase(workspace) == "needs-review"
    files = {item["kind"]: item for item in result["artifacts"]}
    assert {"spec", "crop", "cut", "check", "final", "preview", "log"} <= set(files)
    for item in result["artifacts"]:
        path = workspace / item["path"]
        assert path.parent == folder
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
    log = json.loads((folder / "log.json").read_text())
    commands = [entry["argv"] for entry in log["commands"]]
    assert [argv[5:7] for argv in commands[:2]] == [
        ["tools/platereview/plate.py", "crop"],
        ["tools/platereview/plate.py", "cut"],
    ]
    assert commands[2][5:] == [
        "tools/platereview/finish.py",
        str(folder / "cut.png"),
        str(folder / "final.png"),
    ]
    assert commands[3][5:] == [
        "tools/add_bird.py",
        str(folder / "final.png"),
        "--style",
        "classic",
        "--key",
        "hirundo-neoxena",
        "--source",
        "gould",
        "--url",
        PLATE_URL,
        "--preview",
        str(folder / "preview.png"),
        "--dry-run",
    ]
    assert (log["notes"], log["scan"]["sha256"]) == ("First cut", scan.stem)
    assert log["source_revision"] == 1 and len(log["upstream"]["revision"]) == 40
    assert json.loads((folder / "spec.json").read_text())["scan"] == str(scan)
    # Each file is attributed to the native command that wrote it; inputs and the log to none.
    with store.connect(workspace, readonly=True) as db:
        made = {
            Path(row["path"]).name: json.loads(row["record_json"])
            for row in db.execute("SELECT path, record_json FROM artifacts")
        }
    assert made["spec.json"]["argv"] is None and made["log.json"]["argv"] is None
    assert [made[name]["argv"] for name in ("crop.png", "cut.png", "final.png", "preview.png")] == (
        commands
    )
    assert {record["upstream_revision"] for record in made.values()} == {
        log["upstream"]["revision"]
    }
    assert scan.read_bytes() == original
    # Nothing tracked, untracked or ignored (bytecode, an environment) lands in the checkout.
    assert upstream_status(workspace) == ""


def test_recut_creates_a_new_revision_and_keeps_the_previous_one(downloaded):
    workspace, scan = downloaded
    spec = write_spec(workspace, scan)
    cutout(workspace, "--spec", str(spec))
    first = digests(workspace / "work/cuts/hirundo-neoxena/r001")
    result = cutout(workspace, "--spec", str(write_spec(workspace, scan, remove=[[[0, 0]]])))
    assert result["revision"] == 2
    assert digests(workspace / "work/cuts/hirundo-neoxena/r001") == first
    shown = cli(workspace, "show", "Welcome Swallow", "--json")["cutout"]
    assert shown["revisions"] == [1, 2]
    assert (shown["current"]["revision"], shown["current"]["intact"]) == (2, True)
    with pytest.raises(PermissionError):
        (workspace / "work/cuts/hirundo-neoxena/r001/final.png").write_bytes(b"overwritten")


@pytest.mark.parametrize("step", ["crop", "cut", "finish"])
def test_failed_native_step_keeps_previous_output_and_phase(downloaded, monkeypatch, step):
    workspace, scan = downloaded
    spec = write_spec(workspace, scan)
    cutout(workspace, "--spec", str(spec))
    first = digests(workspace / "work/cuts/hirundo-neoxena/r001")
    monkeypatch.setenv("FIXTURE_FAIL", step)
    error = cutout(workspace, "--spec", str(spec), ok=False)
    assert f"fixture {step} failed" in error and "r002/log.json" in error
    assert phase(workspace) == "needs-review"
    shown = cli(workspace, "show", "Welcome Swallow", "--json")
    assert shown["cutout"]["current"]["revision"] == 1
    assert shown["events"][-1]["action"] == "cutout-failed"
    assert digests(workspace / "work/cuts/hirundo-neoxena/r001") == first
    failed = json.loads((workspace / "work/cuts/hirundo-neoxena/r002/log.json").read_text())
    assert failed["commands"][-1]["returncode"] != 0
    monkeypatch.delenv("FIXTURE_FAIL")
    assert cutout(workspace, "--spec", str(spec))["revision"] == 3


def test_first_cut_failure_leaves_the_bird_downloaded(downloaded, monkeypatch):
    workspace, scan = downloaded
    monkeypatch.setenv("FIXTURE_FAIL", "cut")
    cutout(workspace, "--spec", str(write_spec(workspace, scan)), ok=False)
    assert phase(workspace) == "downloaded"
    assert cli(workspace, "show", "Welcome Swallow", "--json")["cutout"]["current"] is None


def test_preview_failure_stops_at_the_cut_phase(downloaded, monkeypatch):
    workspace, scan = downloaded
    monkeypatch.setenv("FIXTURE_FAIL", "preview")
    assert "fixture preview failed" in cutout(
        workspace, "--spec", str(write_spec(workspace, scan)), ok=False
    )
    assert phase(workspace) == "cut"
    current = cli(workspace, "show", "Welcome Swallow", "--json")["cutout"]["current"]
    kinds = {item["kind"] for item in current["artifacts"]}
    assert "final" in kinds and "preview" not in kinds


def test_cutout_needs_the_approved_downloaded_scan(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    other = workspace / "work/other.png"
    other.write_bytes(png())
    assert "downloaded" in cutout(workspace, "--spec", str(write_spec(workspace, other)), ok=False)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(png()))
    scan = workspace / fetch.acquire(workspace, "Welcome Swallow")["path"]
    for wrong in (other, Path("work/scans") / scan.name):
        error = cutout(workspace, "--spec", str(write_spec(workspace, wrong)), ok=False)
        assert "absolute path of the approved scan" in error
    assert not (workspace / "work/cuts").exists()
    assert phase(workspace) == "downloaded"


def test_warnings_notes_and_disclosures_are_retained(downloaded, monkeypatch):
    workspace, scan = downloaded
    monkeypatch.setenv("FIXTURE_SEVER", "1")
    spec = write_spec(workspace, scan, balance=[240, 228, 210], remove=[[[0, 0], [5, 0], [5, 5]]])
    result = cutout(workspace, "--spec", str(spec), "--notes", "Dropped the upper bird")
    assert result["notes"] == "Dropped the upper bird"
    assert any("not connected to the seed" in line for line in result["warnings"])
    assert any("graded 55%" in line for line in result["disclosures"])
    assert any("balance" in line and "remove" in line for line in result["disclosures"])
    current = cli(workspace, "show", "Welcome Swallow", "--json")["cutout"]["current"]
    assert current["warnings"] == result["warnings"]


def test_manual_export_needs_notes_and_skips_native_cutting(downloaded):
    workspace, scan = downloaded
    export = workspace / "work/hand-cut.png"
    export.write_bytes(png())
    assert "notes" in cutout(workspace, "--manual", str(export), ok=False)
    result = cutout(workspace, "--manual", str(export), "--notes", "Cut by hand in Krita")
    assert result["phase"] == "needs-review"
    kinds = {item["kind"] for item in result["artifacts"]}
    assert kinds == {"manual", "preview", "log"}
    folder = workspace / result["path"]
    assert (folder / "manual.png").read_bytes() == export.read_bytes()
    log = json.loads((folder / "log.json").read_text())
    assert [entry["argv"][5] for entry in log["commands"]] == ["tools/add_bird.py"]
    assert log["scan"]["sha256"] == scan.stem


def test_a_native_tool_that_touches_the_checkout_stops_the_cut(downloaded, monkeypatch):
    workspace, scan = downloaded
    monkeypatch.setenv("FIXTURE_DIRTY", "1")
    error = cutout(workspace, "--spec", str(write_spec(workspace, scan)), ok=False)
    assert "changed the upstream checkout" in error
    assert phase(workspace) == "downloaded"


def test_cutout_refuses_a_blocked_bird(downloaded):
    workspace, scan = downloaded
    cli(workspace, "block", "Welcome Swallow", "--reason", "artwork-quality", "--notes", "Hold")
    assert "unblocked" in cutout(workspace, "--spec", str(write_spec(workspace, scan)), ok=False)


def test_schema_two_workspace_migrates_before_cutting(downloaded):
    workspace, scan = downloaded
    with sqlite3.connect(workspace / ".fugart/state.sqlite3") as db:
        db.execute("DROP TABLE operations")
        db.execute("DROP TABLE artifacts")
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    spec = write_spec(workspace, scan)
    assert "fugart migrate" in cutout(workspace, "--spec", str(spec), ok=False)
    assert cli(workspace, "show", "Welcome Swallow", "--json")["cutout"] is None
    result = cli(workspace, "migrate", "--json")
    assert (result["schema_version"], result["changed"]) == (4, True)
    with sqlite3.connect(result["backup"]) as backup:
        assert backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (
            "2",
        )
    assert phase(workspace) == "downloaded"
    assert cutout(workspace, "--spec", str(spec))["revision"] == 1


def test_rotate_cuts_from_an_upright_copy_and_leaves_the_scan_alone(downloaded):
    workspace, scan = downloaded
    original = scan.read_bytes()
    spec = write_spec(workspace, scan, rotate=90)
    result = cutout(workspace, "--spec", str(spec), "--notes", "Landscape plate bound sideways")
    folder = workspace / "work/cuts/hirundo-neoxena/r001"
    assert scan.read_bytes() == original
    with Image.open(folder / "scan.png") as upright, Image.open(folder / "crop.png") as crop:
        assert upright.size == crop.size == (700, 800)
    used = json.loads((folder / "spec.json").read_text())
    assert used["scan"] == str(folder / "scan.png") and used["rotate"] == 90
    kinds = {item["kind"] for item in result["artifacts"]}
    assert "scan" in kinds
    assert any("rotated 90" in line for line in result["disclosures"])


def test_rotate_rejects_anything_but_quarter_turns(downloaded):
    workspace, scan = downloaded
    spec = write_spec(workspace, scan, rotate=45)
    assert "rotate" in cutout(workspace, "--spec", str(spec), "--notes", "x", ok=False)
    assert phase(workspace) == "downloaded"
