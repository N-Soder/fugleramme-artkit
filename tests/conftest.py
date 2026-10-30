import json
import subprocess
import sys

import pytest

# Stand-ins for upstream's native art tools: same argv contract, stdlib only. They copy
# bytes instead of cutting, so tests exercise this project's orchestration, not upstream.
FAKE_PLATE = """
import json, os, shutil, sys
from pathlib import Path
command, spec_path = sys.argv[1], Path(sys.argv[2])
spec, out = json.loads(spec_path.read_text()), spec_path.parent
if os.environ.get("FIXTURE_FAIL") == command:
    sys.exit(f"fixture {command} failed")
if command == "crop":
    if os.environ.get("FIXTURE_DIRTY"):
        Path("stray.txt").write_text("a native tool wrote into the checkout")
    shutil.copyfile(spec["scan"], out / "crop.png")
    print("crop (800, 700)")
elif command == "cut":
    for name in ("cut.png", "check.png", "check.jpg"):
        shutil.copyfile(out / "crop.png", out / name)
    if os.environ.get("FIXTURE_SEVER"):
        print("  ! 25000 px not connected to the seed - check for a severed leg or tail")
    print("bird 1000 px of 560000, gaps 0 px")
"""
FAKE_FINISH = """
import os, shutil, sys
if os.environ.get("FIXTURE_FAIL") == "finish":
    sys.exit("fixture finish failed")
shutil.copyfile(sys.argv[1], sys.argv[2])
print(sys.argv[2], (800, 700), "ink 52, graded 55%")
"""
FAKE_ADD_BIRD = """
import json, os, shutil, sys
from pathlib import Path
args, fail = sys.argv[1:], os.environ.get("FIXTURE_FAIL")
key = args[args.index("--key") + 1]
style = Path("assets/artwork") / args[args.index("--style") + 1]
number, name = 1, f"{key}.webp"
while (style / "birds" / name).exists():
    number += 1
    name = f"{key}-{number}.webp"
if "--dry-run" in args:
    if fail == "preview":
        sys.exit("fixture preview failed")
    shutil.copyfile(args[0], args[args.index("--preview") + 1])
    print(f"  {args[0]} -> {style}/birds/{name}  (800x700)")
    print("dry run, the style is untouched")
    sys.exit()
if "--no-box" not in args or "--no-detect" not in args:
    sys.exit("fixture would open the box editor")
if fail == "import":
    sys.exit("fixture import failed")
print(f"  {args[0]} -> {style}/birds/{name}  (800x700)")
shutil.copyfile(args[0], style / "birds" / name)
if fail == "manifest":
    sys.exit("fixture died before the manifest")
entry = {"source": args[args.index("--source") + 1], "url": args[args.index("--url") + 1]}
listed = json.loads((style / "manifest.json").read_text()) | {f"birds/{name}": entry}
(style / "manifest.json").write_text(json.dumps(listed, indent=2, sort_keys=True))
if fail == "after":
    sys.exit("fixture died after the manifest")
print("written")
"""


def cli(workspace, *args, ok=True):
    result = subprocess.run(
        [sys.executable, "-m", "fugart.cli", "--workspace", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    return json.loads(result.stdout) if ok else result.stderr


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "upstream"
    paths = {
        "src/fugleramme/__init__.py": "",
        "src/fugleramme/names.py": """
def normalize(name):
    return name.strip().lower().replace(" ", "-").replace("corvus-monedula", "coloeus-monedula")
""",
        "hooks/species.py": """
import json
from pathlib import Path
def rows():
    return json.loads((Path(__file__).resolve().parent.parent / "catalog.json").read_text())
def _labels():
    return [(row.get('label', row['name']), row['common']) for row in rows()
            if row.get('detectable') is not False]
""",
        "catalog.json": json.dumps(
            [
                {
                    "name": "Coloeus monedula",
                    "label": "Corvus monedula",
                    "common": "Jackdaw",
                    "plates": [],
                },
                {"name": "Hirundo neoxena", "common": "Welcome Swallow", "plates": []},
                {
                    "name": "Cygnus atratus",
                    "common": "Black Swan",
                    "plates": ["other/birds/cygnus-atratus.webp"],
                },
                {
                    "name": "Turdus merula",
                    "common": "Common Blackbird",
                    "plates": ["classic/birds/turdus-merula.webp"],
                },
                {"name": "Pardalotus punctatus", "common": "Spotted Pardalote", "plates": []},
                {"name": "Platycercus eximius", "common": "Eastern Rosella", "plates": []},
                {"name": "Chenonetta jubata", "common": "Maned Duck", "plates": []},
                {
                    "name": "Unknown extinctus",
                    "common": "Extinct",
                    "plates": [],
                    "detectable": False,
                },
            ]
        ),
        "assets/birdnet_labels_v2.4.txt": "labels fixture",
        "assets/birdnet_aliases.json": "{}",
        "assets/bird_sizes.csv": "scientific_name,mass_g\n",
        "assets/artwork/classic/manifest.json": "{}",
        "assets/artwork/classic/ATTRIBUTION.md": "Manifest key: `gould`.",
        "assets/artwork/classic/birds/turdus-merula.webp": "fixture",
        "tools/add_bird.py": FAKE_ADD_BIRD,
        "tools/bird_box.py": """
import json, os, sys
from pathlib import Path
def _size(path):
    return (800, 700)
if __name__ == '__main__':
    assert os.environ['BROWSER'] == 'true'
    plate = Path(sys.argv[1])
    geometry = plate.parent.parent / 'geometry.json'
    listed = json.loads(geometry.read_text()) if geometry.exists() else {}
    listed['birds/' + plate.name] = {'box': [0.1, 0.2, 0.8, 0.9], 'cut': [800, 700]}
    geometry.write_text(json.dumps(listed))
    print('saved')
""",
        "tools/preview_plates.py": "",
        "tools/platereview/plate.py": FAKE_PLATE,
        "tools/platereview/finish.py": FAKE_FINISH,
        "tools/platereview/build.py": "",
        "tests/test_artwork_names.py": "",
        "tests/test_artwork_duplicates.py": "",
        "pyproject.toml": '[project]\nname = "fixture-upstream"\nversion = "0"\n'
        'requires-python = ">=3.12"\n',
    }
    for name, content in paths.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    # Native art tools run in the checkout's locked uv environment; give the fixture one.
    subprocess.run(["uv", "lock", "--offline", "-q"], cwd=root, check=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
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
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return root


def checklist(path, names=None):
    names = names or ["Corvus monedula", "Hirundo neoxena", "Cygnus atratus", "Turdus merula"]
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_url": "https://example.invalid/au-checklist",
                "retrieved_at": "2026-10-05T00:00:00+00:00",
                "scope": "AU pilot fixture",
                "complete": True,
                "species": [
                    {"scientific_name": name, "records": i + 25} for i, name in enumerate(names)
                ],
            }
        )
    )
    return path


@pytest.fixture
def initialized(tmp_path, checkout):
    workspace = tmp_path / "companion"
    workspace.mkdir()
    cli(workspace, "init", "--upstream", str(checkout), "--json")
    return workspace


@pytest.fixture
def source_workspace(initialized, tmp_path):
    cli(initialized, "inventory", "--checklist", str(checklist(tmp_path / "au.json")), "--json")
    cli(initialized, "migrate", "--json")
    evidence = initialized / "work" / "metadata.json"
    evidence.write_text('{"rights":"public domain","subject":"Hirundo neoxena"}')
    source = {
        "schema_version": 1,
        "scientific_name": "Hirundo neoxena",
        "historical_name": "Hirundo neoxena",
        "work": "The Birds of Australia",
        "work_date": "1840-1848",
        "creators": ["John Gould", "Henry Constantine Richter"],
        "provider": "Smithsonian Libraries via Internet Archive",
        "source_key": "gould",
        "plate_url": "https://archive.org/details/birdsAustraliav2Goul/page/n36",
        "download_url": "https://archive.org/download/birdsAustraliav2Goul/page/n36.jpg",
        "licence": "Public domain",
        "licence_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
        "licence_evidence": "Provider records public-domain scan of the original work.",
        "taxonomy_notes": "Plate caption and current label both name Hirundo neoxena.",
        "taxonomy_urls": ["https://itis.gov/servlet/SingleRpt/SingleRpt?search_value=560475"],
        "evidence": [
            {"url": "https://commons.wikimedia.org/wiki/File:Example.jpg", "file": "metadata.json"}
        ],
        "notes": "Fixture, not real source approval.",
    }
    source_file = initialized / "work" / "source.json"
    source_file.write_text(json.dumps(source))
    return initialized, source_file
