import json
import sqlite3
from pathlib import Path

import pytest
from conftest import checklist, cli


def record(workspace, path):
    return cli(workspace, "source", "record", "Hirundo neoxena", "--file", str(path), "--json")


def approve(workspace, revision="1", ok=True):
    return cli(
        workspace,
        "source",
        "approve",
        "Hirundo neoxena",
        "--revision",
        revision,
        "--reviewer",
        "Test human",
        "--notes",
        "Verified source, species and licence",
        "--confirm-source-species-licence",
        "--json",
        ok=ok,
    )


def test_sources_are_immutable_and_source_edits_invalidate_approval(source_workspace):
    workspace, path = source_workspace
    source = record(workspace, path)
    assert source["revision"] == 1
    assert source["approval"] is None
    assert cli(workspace, "show", "Welcome Swallow", "--json")["state"] == "researching"
    assert record(workspace, path)["changed"] is False
    approved = approve(workspace)
    assert approved["approval"]["reviewer"] == "Test human"
    assert cli(workspace, "show", "Welcome Swallow", "--json")["state"] == "sourced"
    assert approve(workspace)["changed"] is False
    data = json.loads(path.read_text())
    data["notes"] = "New rationale requires human review"
    path.write_text(json.dumps(data))
    assert record(workspace, path)["revision"] == 2
    assert "current revision" in approve(workspace, ok=False)
    assert cli(workspace, "show", "Welcome Swallow", "--json")["state"] == "researching"
    history = cli(workspace, "source", "show", "Welcome Swallow", "--json")
    assert len(history["history"]) == 2
    assert history["current"]["approval"] is None


def test_record_copies_evidence_and_detects_corruption(source_workspace):
    workspace, path = source_workspace
    source = record(workspace, path)
    frozen = workspace / source["record"]["evidence"][0]["path"]
    assert frozen.read_bytes() == (workspace / "work/metadata.json").read_bytes()
    (workspace / "work/metadata.json").write_text("changed original metadata")
    assert frozen.read_text().startswith('{"rights"')
    frozen.write_text("corrupted evidence")
    assert "checksum" in approve(workspace, ok=False)


def test_source_approval_refuses_newly_covered_bird(source_workspace):
    workspace, path = source_workspace
    record(workspace, path)
    with sqlite3.connect(workspace / ".fugart/state.sqlite3") as db:
        db.execute(
            "UPDATE species SET artwork='[\"classic/birds/hirundo-neoxena.webp\"]' WHERE key='hirundo-neoxena'"
        )
    assert "covered" in approve(workspace, ok=False)


def test_current_name_change_requires_new_source_record(source_workspace):
    workspace, path = source_workspace
    record(workspace, path)
    with sqlite3.connect(workspace / ".fugart/state.sqlite3") as db:
        db.execute("UPDATE species SET scientific_name='Changed taxon' WHERE key='hirundo-neoxena'")
    assert "scientific name changed" in approve(workspace, ok=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("licence", ""),
        ("taxonomy_notes", ""),
        ("creators", []),
        ("scientific_name", "Cygnus atratus"),
        ("download_url", "http://127.0.0.1/secret"),
        ("evidence", []),
    ],
)
def test_invalid_sources_do_not_change_species(source_workspace, field, value):
    workspace, path = source_workspace
    source = json.loads(path.read_text())
    source[field] = value
    path.write_text(json.dumps(source))
    cli(workspace, "source", "record", "Hirundo neoxena", "--file", str(path), ok=False)
    assert cli(workspace, "show", "Welcome Swallow", "--json")["state"] == "missing"


def test_blocking_retains_phase_and_requires_notes(source_workspace):
    workspace, path = source_workspace
    record(workspace, path)
    result = cli(
        workspace,
        "block",
        "Welcome Swallow",
        "--reason",
        "licensing-uncertainty",
        "--notes",
        "Need clearer licence evidence",
        "--json",
    )
    assert result["state"] == "researching"
    assert result["blocked_reason"] == "licensing-uncertainty"
    assert "blocked" in approve(workspace, ok=False)
    cli(workspace, "unblock", "Welcome Swallow", "--notes", "Resolved by source review", "--json")
    approve(workspace)
    assert cli(workspace, "show", "Welcome Swallow", "--json")["state"] == "sourced"


def test_schema_one_migration_preserves_backlog_and_makes_backup(initialized, tmp_path):
    cli(initialized, "inventory", "--checklist", str(checklist(tmp_path / "au.json")), "--json")
    # Recreate a milestone-1 database by removing additive tables and setting its version.
    with sqlite3.connect(initialized / ".fugart/state.sqlite3") as db:
        for name in [
            "operations",
            "artifacts",
            "fetch_jobs",
            "downloads",
            "source_reviews",
            "sources",
        ]:
            db.execute(f"DROP TABLE IF EXISTS {name}")
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    result = cli(initialized, "migrate", "--json")
    assert (result["schema_version"], result["changed"]) == (4, True)
    assert Path(result["backup"]).is_file()
    with sqlite3.connect(result["backup"]) as backup:
        assert (
            backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
        )
        assert backup.execute("SELECT COUNT(*) FROM species").fetchone()[0] == 4
    assert cli(initialized, "status", "--json")["total"] == 4
    assert cli(initialized, "migrate", "--json")["changed"] is False
