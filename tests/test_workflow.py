import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from conftest import checklist, cli


def test_init_is_idempotent_and_rebinding_is_rejected(initialized, checkout, tmp_path):
    db = initialized / ".fugart/state.sqlite3"
    before = db.read_bytes()
    result = cli(initialized, "init", "--upstream", str(checkout), "--json")
    assert result["created"] is False
    assert db.read_bytes() == before
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(checkout), str(other)], check=True)
    assert "already bound" in cli(initialized, "init", "--upstream", str(other), ok=False)
    assert db.read_bytes() == before


def test_read_before_init_does_not_create_state(tmp_path):
    assert "init" in cli(tmp_path, "status", "--json", ok=False)
    assert not (tmp_path / ".fugart").exists()


def test_reject_nested_repositories_and_invalid_checkout(tmp_path, checkout):
    assert "disjoint" in cli(checkout, "init", "--upstream", str(checkout), ok=False)
    assert not (checkout / ".fugart").exists()
    assert "checkout" in cli(tmp_path, "init", "--upstream", str(tmp_path / "absent"), ok=False)


def test_inventory_native_aliases_style_coverage_and_repeat(initialized, tmp_path, checkout):
    path = checklist(tmp_path / "au.json")
    result = cli(initialized, "inventory", "--checklist", str(path), "--json")
    assert (result["total"], result["covered"], result["missing"]) == (4, 1, 3)
    assert cli(initialized, "inventory", "--checklist", str(path), "--json")["changed"] is False
    row = cli(initialized, "show", "Corvus monedula", "--json")
    assert row["key"] == "coloeus-monedula"
    assert row["scientific_name"] == "Coloeus monedula"
    assert row["label_names"] == ["Corvus monedula"]
    assert cli(initialized, "show", "Black Swan", "--json")["artwork"] == []
    assert cli(initialized, "show", "Common Blackbird", "--json")["state"] is None
    assert subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain"]) == b""


def test_refresh_preserves_progress_and_retires_absent_records(initialized, tmp_path):
    path = checklist(tmp_path / "au.json")
    cli(initialized, "inventory", "--checklist", str(path), "--json")
    with sqlite3.connect(initialized / ".fugart/state.sqlite3") as db:
        db.execute(
            "UPDATE species SET state='researching', blocked_reason='taxonomy-ambiguity', notes='mapping' WHERE key='hirundo-neoxena'"
        )
    checklist(path, ["Hirundo neoxena", "Cygnus atratus"])
    cli(initialized, "inventory", "--checklist", str(path), "--json")
    row = cli(initialized, "show", "Welcome Swallow", "--json")
    assert (row["state"], row["blocked_reason"], row["notes"]) == (
        "researching",
        "taxonomy-ambiguity",
        "mapping",
    )
    assert cli(initialized, "show", "Jackdaw", "--json")["active"] is False
    assert cli(initialized, "next", "--json")["key"] == "cygnus-atratus"


@pytest.mark.parametrize("bad_name", ["Not a bird", "Unknown extinctus"])
def test_unknown_checklist_is_atomic(initialized, tmp_path, bad_name):
    before = (initialized / ".fugart/state.sqlite3").read_bytes()
    path = checklist(tmp_path / "bad.json", ["Hirundo neoxena", bad_name])
    assert "not a detectable" in cli(initialized, "inventory", "--checklist", str(path), ok=False)
    assert (initialized / ".fugart/state.sqlite3").read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("complete", False),
        ("species", []),
        ("source_url", ""),
        ("retrieved_at", "yesterday"),
        ("schema_version", 99),
    ],
)
def test_invalid_checklist_is_atomic(initialized, tmp_path, field, value):
    path = checklist(tmp_path / "bad.json")
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    before = (initialized / ".fugart/state.sqlite3").read_bytes()
    cli(initialized, "inventory", "--checklist", str(path), ok=False)
    assert (initialized / ".fugart/state.sqlite3").read_bytes() == before


def test_batches_are_small_immutable_and_idempotent(initialized, tmp_path):
    path = checklist(
        tmp_path / "au.json",
        [
            "Hirundo neoxena",
            "Cygnus atratus",
            "Pardalotus punctatus",
            "Platycercus eximius",
            "Chenonetta jubata",
            "Coloeus monedula",
            "Turdus merula",
        ],
    )
    cli(initialized, "inventory", "--checklist", str(path), "--json")
    birds = [
        "Hirundo neoxena",
        "Cygnus atratus",
        "Pardalotus punctatus",
        "Platycercus eximius",
        "Chenonetta jubata",
    ]
    assert "five" in cli(initialized, "batch", "create", "too-big", *birds, "Jackdaw", ok=False)
    assert "covered" in cli(initialized, "batch", "create", "covered", "Common Blackbird", ok=False)
    first = cli(initialized, "batch", "create", "pilot-01", *birds, "--json")
    assert len(first["members"]) == 5
    assert (
        cli(initialized, "batch", "create", "pilot-01", *reversed(birds), "--json")["created"]
        is False
    )
    assert "different members" in cli(
        initialized, "batch", "create", "pilot-01", "Jackdaw", ok=False
    )
    assert "already in batch" in cli(initialized, "batch", "create", "pilot-02", birds[0], ok=False)
    assert len(cli(initialized, "batch", "show", "pilot-01", "--json")["members"]) == 5
    assert cli(initialized, "next", "--json")["key"] == "coloeus-monedula"
    assert cli(initialized, "next", "--batch", "pilot-01", "--json")["key"] == "chenonetta-jubata"


def test_schema_version_is_checked_before_write(initialized, checkout):
    with sqlite3.connect(initialized / ".fugart/state.sqlite3") as db:
        db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    before = (initialized / ".fugart/state.sqlite3").read_bytes()
    assert "schema" in cli(initialized, "init", "--upstream", str(checkout), ok=False)
    assert (initialized / ".fugart/state.sqlite3").read_bytes() == before


def test_actual_upstream_inventory(tmp_path):
    upstream = os.environ.get("FUGART_TEST_UPSTREAM")
    if not upstream:
        pytest.skip("set FUGART_TEST_UPSTREAM for the explicit real-checkout smoke test")
    cli(tmp_path, "init", "--upstream", upstream, "--json")
    path = Path(__file__).resolve().parents[1] / "examples/au-2026-10-05.json"
    result = cli(tmp_path, "inventory", "--checklist", str(path), "--json")
    assert result["total"] == 545
    assert result["covered"] + result["missing"] == 545
    assert cli(tmp_path, "inventory", "--checklist", str(path), "--json")["changed"] is False
