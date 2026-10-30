"""Read a separate checkout using Fugleramme's own species/name helpers."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REQUIRED = (
    "src/fugleramme/names.py",
    "hooks/species.py",
    "assets/birdnet_labels_v2.4.txt",
    "assets/birdnet_aliases.json",
    "assets/bird_sizes.csv",
    "assets/artwork/classic/manifest.json",
    "assets/artwork/classic/ATTRIBUTION.md",
    "tools/add_bird.py",
    "tools/bird_box.py",
    "tools/preview_plates.py",
    "tools/platereview/plate.py",
    "tools/platereview/finish.py",
    "tools/platereview/build.py",
    "tests/test_artwork_names.py",
    "tests/test_artwork_duplicates.py",
)


def run(argv: list[str], root: Path, **kwargs) -> str:
    result = subprocess.run(
        argv, cwd=root, capture_output=True, text=True, timeout=60, check=False, **kwargs
    )
    if result.returncode:
        raise ValueError(f"upstream command failed: {result.stderr.strip()}")
    return result.stdout.strip()


def validate_checkout(root: Path, workspace: Path) -> None:
    if root == workspace or root in workspace.parents or workspace in root.parents:
        raise ValueError("the companion and Fugleramme checkout must be disjoint")
    if not root.is_dir():
        raise ValueError(f"no Fugleramme checkout at {root}")
    top = run(["git", "rev-parse", "--show-toplevel"], root)
    if Path(top).resolve() != root:
        raise ValueError("--upstream must name the Git checkout root")
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing or not (root / "assets/artwork/classic/birds").is_dir():
        raise ValueError(f"incompatible Fugleramme checkout; missing: {', '.join(missing)}")


# The native hook owns label folding, bird filtering and artwork enumeration.
# Imports run in a child process so a second checkout cannot reuse module caches.
PROBE = """
import json, sys
from fugleramme.names import normalize
import species
rows = {normalize(row['name']): row for row in species.rows()
        if row.get('detectable') is not False}
labels = {}
for scientific, common in species._labels():
    labels.setdefault(normalize(scientific), []).append(scientific)
result = []
for name in json.load(sys.stdin):
    key = normalize(name)
    result.append({'requested': name, 'key': key, 'row': rows.get(key),
                   'label_names': labels.get(key, [])})
print(json.dumps(result))
"""


def inventory(root: Path, names: list[str]) -> dict:
    revision = run(["git", "rev-parse", "HEAD"], root)
    dirty = bool(run(["git", "status", "--porcelain"], root))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "hooks")])}
    output = run([sys.executable, "-B", "-c", PROBE], root, env=env, input=json.dumps(names))
    rows = json.loads(output)
    unknown = [row["requested"] for row in rows if row["row"] is None]
    if unknown:
        raise ValueError(f"not a detectable BirdNET bird: {', '.join(unknown)}")
    digest = hashlib.sha256()
    for path in sorted(
        set(REQUIRED)
        | {"src/fugleramme/taxa.py", "src/fugleramme/config.py", "src/fugleramme/picks.py"}
    ):
        file = root / path
        if file.exists():
            digest.update(path.encode())
            digest.update(file.read_bytes())
    digest.update(json.dumps(rows, sort_keys=True).encode())
    return {"revision": revision, "dirty": dirty, "fingerprint": digest.hexdigest(), "rows": rows}


# Joined by name and label spellings, detectable rows only, as species.js joins a
# country on the coverage page, so a built checklist and that page agree.
JOIN = """
import json, sys
import species
by = {}
for row in species.rows():
    if row.get('detectable') is False:
        continue
    for name in [row['name'], *filter(None, str(row.get('label', '')).split(', '))]:
        by[name.lower()] = row['name']
found, unmatched = {}, 0
for binomial, count in json.load(sys.stdin).items():
    name = by.get(binomial.lower())
    if name:
        found[name] = found.get(name, 0) + count
    else:
        unmatched += 1
print(json.dumps({'species': found, 'unmatched': unmatched}))
"""


def detectable(root: Path, counts: dict[str, int]) -> dict:
    """GBIF binomials -> {canonical name: records} for birds BirdNET can hear."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "hooks")])}
    output = run([sys.executable, "-B", "-c", JOIN], root, env=env, input=json.dumps(counts))
    return json.loads(output)


def status(root: Path) -> str:
    return run(["git", "status", "--porcelain"], root)


def changed_paths(root: Path) -> list[str]:
    """Every path that differs from HEAD, tracked or not; empty for a clean checkout."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return sorted(entry[3:] for entry in result.stdout.split("\0") if entry)


def native(root: Path, workspace: Path, script: str, *args: str) -> dict:
    """Run one native art tool in upstream's locked environment and return its log.

    The environment and bytecode stay in the companion workspace, so the checkout
    is only ever read.
    """
    argv = ["uv", "run", "--locked", "python", "-B", script, *args]
    env = {name: value for name, value in os.environ.items() if name != "VIRTUAL_ENV"} | {
        "UV_PROJECT_ENVIRONMENT": str(workspace / ".fugart/upstream-env"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    started = datetime.now(UTC).isoformat()
    result = subprocess.run(
        argv, cwd=root, capture_output=True, text=True, timeout=900, check=False, env=env
    )
    return {
        "argv": argv,
        "started_at": started,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
