import json
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import cli

from fugart import checklist

COUNTRIES = [{"iso2": "AU", "title": "Australia"}, {"iso2": "NZ", "title": "New Zealand"}]


def gbif(pages):
    """A stand-in for GBIF: the country list, then one facet page per offset."""
    asked = []

    def fetch(url):
        asked.append(url)
        if url.endswith("/enumeration/country"):
            return json.dumps(COUNTRIES).encode()
        offset = int(parse_qs(urlparse(url).query)["facetOffset"][0])
        counts = pages[offset // checklist.PAGE]
        return json.dumps({"facets": [{"counts": counts}]}).encode()

    return fetch, asked


FACETS = [
    {"name": "Hirundo neoxena Gould, 1842", "count": 900},
    {"name": "Corvus monedula Linnaeus, 1758", "count": 40},  # old label spelling
    {"name": "Coloeus monedula (Linnaeus, 1758)", "count": 2},
    {"name": "Anas platyrhynchos x superciliosa", "count": 80},  # hybrid
    {"name": "Unknown extinctus", "count": 30},  # not detectable
    {"name": "Nonexistent bird", "count": 30},
]


def test_build_joins_gbif_to_detectable_birds(initialized, tmp_path):
    fetch, asked = gbif([FACETS])
    out = tmp_path / "au.json"
    result = checklist.build(initialized, "au", years="2017,2026", output=out, fetch=fetch)
    data = json.loads(out.read_text())
    assert data["species"] == [
        {"scientific_name": "Coloeus monedula", "records": 42},
        {"scientific_name": "Hirundo neoxena", "records": 900},
    ]
    assert data["region"] == "Australia" and data["complete"] is True
    query = parse_qs(urlparse(asked[1]).query)
    assert query["country"] == ["AU"] and query["year"] == ["2017,2026"]
    assert query["facetMincount"] == ["25"] and query["basisOfRecord"] == ["HUMAN_OBSERVATION"]
    assert result["detectable"] == 2 and result["unmatched"] == 2
    assert len(data["raw_sha256"]) == 1
    assert (initialized / "work/checklists").is_dir()
    # The built checklist feeds inventory unchanged, and the UI can name the region.
    inventory = cli(initialized, "inventory", "--checklist", str(out), "--json")
    assert inventory["total"] == 2 and inventory["region"] == "Australia"


def test_build_pages_past_the_facet_cap(initialized, tmp_path):
    full = [{"name": f"Filler bird{i:04d}", "count": 30} for i in range(checklist.PAGE)]
    fetch, asked = gbif([full, FACETS[:1]])
    result = checklist.build(initialized, "AU", output=tmp_path / "a.json", fetch=fetch)
    assert len(asked) == 3 and result["detectable"] == 1


def test_build_refuses_a_partial_answer(initialized, tmp_path):
    full = [{"name": f"Filler bird{i:04d}", "count": 30} for i in range(checklist.PAGE)]
    fetch, _ = gbif([full, full, full])
    with pytest.raises(ValueError, match="partial"):
        checklist.build(initialized, "AU", output=tmp_path / "a.json", fetch=fetch)
    assert not (tmp_path / "a.json").exists()


@pytest.mark.parametrize(
    ("country", "options", "error"),
    [
        ("Australia", {}, "two-letter"),
        ("ZZ", {}, "does not know"),
        ("AU", {"years": "2026"}, "years"),
        ("AU", {"years": "2026,2017"}, "years"),
        ("AU", {"min_records": 0}, "min-records"),
        ("AU", {"gadm": "Western Australia"}, "GADM"),
    ],
)
def test_build_rejects_bad_input(initialized, tmp_path, country, options, error):
    fetch, _ = gbif([FACETS])
    with pytest.raises(ValueError, match=error):
        checklist.build(initialized, country, output=tmp_path / "a.json", fetch=fetch, **options)


def test_build_narrows_to_a_subdivision(initialized, tmp_path):
    fetch, asked = gbif([FACETS])
    result = checklist.build(
        initialized, "AU", gadm="AUS.11_1", output=tmp_path / "wa.json", fetch=fetch
    )
    query = parse_qs(urlparse(asked[1]).query)
    assert query["gadmGid"] == ["AUS.11_1"] and "country" not in query
    assert result["region"] == "Australia (AUS.11_1)"
