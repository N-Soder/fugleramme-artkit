"""Build a dated checklist for any country from GBIF, as upstream's coverage page does.

The query copies `docs/assets/species.js` in the Fugleramme checkout: human
observations of birds with coordinates over the last ten years, one facet per
scientific name, with a floor below which a bird counts as a vagrant. Names are
then joined to BirdNET's detectable birds by upstream's own species rows, so the
result is the backlog a frame in that place could hear.
"""

import hashlib
import json
import re
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from . import store, upstream

GBIF = "https://api.gbif.org/v1"
PAGE = 1200  # GBIF's facet cap; a full last page means a missing tail
MAX_OFFSET = 3600
REGULAR = 25  # upstream's default: fewer records in a decade is a vagrant
AVES = 212


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "fugleramme-artkit"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def parse_years(value: str | None, now: datetime) -> tuple[int, int]:
    if value is None:
        return now.year - 9, now.year
    match = re.fullmatch(r"(\d{4}),(\d{4})", value)
    if not match or int(match[1]) > int(match[2]):
        raise ValueError("years must look like 2017,2026")
    return int(match[1]), int(match[2])


def query_url(where: dict, years: tuple[int, int], floor: int, offset: int) -> str:
    params = {
        "taxonKey": AVES,
        "facet": "scientificName",
        "facetLimit": PAGE,
        "facetOffset": offset,
        "facetMincount": floor,
        "limit": 0,
        "occurrenceStatus": "PRESENT",
        "basisOfRecord": "HUMAN_OBSERVATION",
        "hasCoordinate": "true",
        "year": f"{years[0]},{years[1]}",
        **where,
    }
    return f"{GBIF}/occurrence/search?{urlencode(params)}"


def fold(values: list[dict], counts: dict[str, int]) -> None:
    """GBIF appends the author and lists hybrids; keep binomials, as species.js does."""
    for value in values:
        name = value["name"]
        if re.search(r" x |×", name):
            continue
        binomial = re.match(r"^[A-Z][a-z]+ [a-z-]+", name)
        if binomial:
            counts[binomial[0]] = counts.get(binomial[0], 0) + value["count"]


def build(
    workspace: Path,
    country: str,
    *,
    gadm: str | None = None,
    years: str | None = None,
    min_records: int = REGULAR,
    output: Path | None = None,
    fetch: Callable[[str], bytes] = fetch_bytes,
) -> dict:
    country = country.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be a two-letter ISO code, such as NZ or GB")
    if gadm is not None and not re.fullmatch(r"[A-Z]{3}(\.\d+)+_\d+", gadm):
        raise ValueError("gadm must be a GADM id, such as AUS.11_1")
    if min_records < 1:
        raise ValueError("min-records must be at least 1")
    now = datetime.now(UTC)
    span = parse_years(years, now)
    with store.connect(workspace, readonly=True) as db:
        root = Path(store.config(db)["upstream_path"])
    upstream.validate_checkout(root, workspace)

    known = {row["iso2"]: row["title"] for row in json.loads(fetch(f"{GBIF}/enumeration/country"))}
    if country not in known:
        raise ValueError(f"GBIF does not know the country code {country}")
    region = known[country]
    if gadm:
        region = f"{region} ({gadm})"
    slug = f"{country.lower()}{'-' + gadm.lower() if gadm else ''}-{now.date().isoformat()}"
    evidence = workspace / "work/checklists" / slug
    where = {"gadmGid": gadm} if gadm else {"country": country}

    counts: dict[str, int] = {}
    urls, hashes, raw = [], {}, {}
    complete = False
    for offset in range(0, MAX_OFFSET, PAGE):
        url = query_url(where, span, min_records, offset)
        body = fetch(url)
        values = (json.loads(body).get("facets") or [{}])[0].get("counts", [])
        name = f"gbif-{offset}.json"
        urls.append(url)
        raw[name] = body
        hashes[name] = hashlib.sha256(body).hexdigest()
        fold(values, counts)
        if len(values) < PAGE:
            complete = True
            break
    if not complete:
        raise ValueError(f"GBIF listed more than {MAX_OFFSET} names; refusing a partial checklist")
    if not counts:
        raise ValueError(f"GBIF lists no birds for {region} with these filters")

    joined = upstream.detectable(root, counts)
    revision = upstream.run(["git", "rev-parse", "HEAD"], root)
    species = [
        {"scientific_name": name, "records": records}
        for name, records in sorted(joined["species"].items())
    ]
    result = {
        "schema_version": 1,
        "source_url": f"{GBIF}/occurrence/search",
        "retrieved_at": now.isoformat(),
        "region": region,
        "scope": (
            f"{region}; GBIF human observations with coordinates, PRESENT, Aves, "
            f"{span[0]}-{span[1]}; >={min_records} records per scientific-name facet; "
            "joined to detectable BirdNET birds by upstream's species rows."
        ),
        "upstream_revision": revision,
        "query_urls": urls,
        "raw_sha256": hashes,
        "complete": True,
        "species": species,
    }
    evidence.mkdir(parents=True, exist_ok=True)
    for name, body in raw.items():
        (evidence / name).write_bytes(body)
    path = (output or evidence.with_suffix(".json")).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    return {
        "checklist": str(path),
        "evidence": str(evidence),
        "region": region,
        "years": f"{span[0]}-{span[1]}",
        "gbif_names": len(counts),
        "detectable": len(species),
        "unmatched": joined["unmatched"],
    }
