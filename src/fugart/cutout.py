"""Cut revisions made only by upstream's native plate tools, kept immutable under work/."""

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from . import fetch, sources, store, upstream

PLATE = "tools/platereview/plate.py"
FINISH = "tools/platereview/finish.py"
ADD_BIRD = "tools/add_bird.py"
PHASES = ("downloaded", "cut", "needs-review")
# A landscape plate bound sideways: degrees clockwise that set the scan upright.
TURNS = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}
KINDS = {
    "spec.json": "spec",
    "scan.png": "scan",
    "crop.png": "crop",
    "cut.png": "cut",
    "check.png": "check",
    "check.jpg": "check",
    "final.png": "final",
    "preview.png": "preview",
    "log.json": "log",
}
FAILURES = (ValueError, OSError, subprocess.SubprocessError)


def approved_scan(db: sqlite3.Connection, workspace: Path, bird: dict) -> tuple[dict, dict]:
    source = sources.current(db, bird["key"])
    if (
        not source
        or not source["approval"]
        or source["approval"]["fingerprint"] != source["fingerprint"]
    ):
        raise ValueError("cutout requires human source approval of the current revision")
    sources.verify_identity(bird, source)
    row = db.execute(
        "SELECT record_json FROM downloads WHERE source_id=?", (source["id"],)
    ).fetchone()
    if bird["state"] not in PHASES or not row:
        raise ValueError("cutout requires the downloaded scan of the approved source")
    scan = json.loads(row[0])
    path = fetch.scan_path(workspace, scan["path"])
    if not path.is_file() or sources.sha256(path) != scan["sha256"]:
        raise ValueError("original scan checksum mismatch; refusing to cut")
    return source, scan


def allocate(db: sqlite3.Connection, workspace: Path, key: str) -> tuple[int, Path]:
    """A new revision folder. Numbers left by failed or interrupted runs are never reused."""
    parent = workspace / "work/cuts" / key
    used = [int(p.name[1:]) for p in parent.glob("r*") if re.fullmatch(r"r\d{3,}", p.name)]
    recorded = db.execute(
        "SELECT MAX(revision) FROM artifacts WHERE species_key=?", (key,)
    ).fetchone()[0]
    revision = max([*used, recorded or 0]) + 1
    folder = parent / f"r{revision:03}"
    folder.mkdir(parents=True)
    return revision, folder


def stage_spec(spec: dict, raw: bytes, scan_file: Path, folder: Path) -> str | None:
    """Write a revision's spec.json; returns the disclosure when the scan had to be turned."""
    if "rotate" in spec and spec["rotate"] not in TURNS:
        raise ValueError("spec rotate must be 90, 180 or 270 (degrees clockwise)")
    note = None
    if turn := spec.get("rotate"):
        # plate.py cannot turn a scan, so it cuts a lossless upright copy; the
        # box and every other coordinate in the spec are in that copy's pixels
        upright = folder / "scan.png"
        with Image.open(scan_file) as original:
            original.convert("RGB").transpose(TURNS[turn]).save(upright)
        raw = json.dumps(spec | {"scan": str(upright)}).encode()
        note = f"scan rotated {turn} degrees clockwise into {upright.name} before cutting"
    (folder / "spec.json").write_bytes(raw)
    return note


def register(db, workspace: Path, job: dict, *, final: bool) -> None:
    """Hash, write-protect and record the revision's new files; the log only once it is final."""
    for path in sorted(job["folder"].iterdir()):
        relative = str(path.relative_to(workspace))
        if (path.name == "log.json" and not final) or db.execute(
            "SELECT 1 FROM artifacts WHERE path=?", (relative,)
        ).fetchone():
            continue
        path.chmod(0o444)
        kind = KINDS.get(path.name, "manual" if path.stem == "manual" else "aux")
        record = {
            "upstream_revision": job["log"]["upstream"]["revision"],
            "argv": job["made"].get(path.name),
        }
        db.execute(
            """INSERT INTO artifacts (species_key, revision, kind, path, sha256, source_id,
            record_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job["key"],
                job["log"]["revision"],
                kind,
                relative,
                sources.sha256(path),
                job["source_id"],
                json.dumps(record),
                datetime.now(UTC).isoformat(),
            ),
        )


def write_log(job: dict) -> None:
    (job["folder"] / "log.json").write_text(json.dumps(job["log"], indent=2, ensure_ascii=False))


def tool(job: dict, script: str, *args: str, expect: str) -> dict:
    """Run one native command; files it leaves in the revision folder are attributed to it."""
    entry = upstream.native(job["root"], job["workspace"], script, *args)
    log = job["log"]
    log["commands"].append(entry)
    log["warnings"] += [
        line.strip() for line in entry["stdout"].splitlines() if line.strip().startswith("!")
    ]
    write_log(job)
    for path in job["folder"].iterdir():
        job["made"].setdefault(path.name, entry["argv"])
    if entry["returncode"]:
        raise ValueError(f"{script} failed: {entry['stderr'].strip()[-800:]}")
    if upstream.status(job["root"]) != job["upstream_status"]:
        raise ValueError(f"{script} changed the upstream checkout; inspect it before retrying")
    if not (job["folder"] / expect).is_file():
        raise ValueError(f"{script} did not write {expect}")
    return entry


def fail(db, job: dict, action: str, error: Exception) -> ValueError:
    job["log"]["error"] = str(error)
    write_log(job)
    place = job["folder"].relative_to(job["workspace"])
    sources.event(db, job["key"], action, f"{place}: {error}")
    return ValueError(f"{error}; see {place}/log.json")


def run(
    workspace: Path, identifier: str, spec_path: Path | None, manual: Path | None, notes: str
) -> dict:
    if manual and not notes.strip():
        raise ValueError("a manual export needs --notes describing the edit")
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_artifacts(db)
        bird = store.find_species(db, identifier)
        if not bird["active"] or bird["blocked_reason"] or bird["artwork"]:
            raise ValueError("cutout requires an active, unblocked bird missing artwork")
        source, scan = approved_scan(db, workspace, bird)
        scan_file = fetch.scan_path(workspace, scan["path"])
        if manual:
            spec, raw = {}, b""
            if not manual.is_file():
                raise ValueError(f"no manual export at {manual}")
        else:
            raw = spec_path.read_bytes()
            spec = json.loads(raw)
            if not isinstance(spec, dict) or spec.get("scan") != str(scan_file):
                raise ValueError(
                    f"spec scan must be the absolute path of the approved scan: {scan_file}"
                )
            if "rotate" in spec and spec["rotate"] not in TURNS:
                raise ValueError("spec rotate must be 90, 180 or 270 (degrees clockwise)")
        settings = store.config(db)
        root = Path(settings["upstream_path"])
        upstream.validate_checkout(root, workspace)
        # --key skips add_bird's interactive search, so the native helpers confirm it first.
        native = upstream.inventory(root, [bird["scientific_name"]])
        if native["rows"][0]["key"] != bird["key"]:
            raise ValueError("upstream no longer files this species under its key; rerun inventory")
        revision, folder = allocate(db, workspace, bird["key"])
        job = {
            "workspace": workspace,
            "root": root,
            "folder": folder,
            "key": bird["key"],
            "source_id": source["id"],
            "upstream_status": upstream.status(root),
            "made": {"log.json": None},
            "log": {
                "species": bird["key"],
                "revision": revision,
                "source_revision": source["revision"],
                "scan": {"path": scan["path"], "sha256": scan["sha256"]},
                "upstream": {"revision": native["revision"], "dirty": native["dirty"]},
                "notes": notes.strip(),
                "warnings": [],
                "disclosures": [],
                "commands": [],
            },
        }
        log = job["log"]
        try:
            if manual:
                image = folder / f"manual{manual.suffix.lower()}"
                shutil.copyfile(manual, image)
                job["made"][image.name] = None
                log["disclosures"].append(f"manual export, not cut by plate.py: {manual.name}")
            else:
                if note := stage_spec(spec, raw, scan_file, folder):
                    job["made"]["scan.png"] = None
                    log["disclosures"].append(note)
                job["made"]["spec.json"] = None
                image = folder / "final.png"
                tool(job, PLATE, "crop", str(folder / "spec.json"), expect="crop.png")
                tool(job, PLATE, "cut", str(folder / "spec.json"), expect="cut.png")
                finish = tool(job, FINISH, str(folder / "cut.png"), str(image), expect=image.name)
                log["disclosures"] += [f"finish.py: {finish['stdout'].strip()}"]
                if hints := sorted(set(spec) - {"scan", "box", "paper_min", "seed", "rotate"}):
                    log["disclosures"].append(f"spec hints used: {', '.join(hints)}")
            if sources.sha256(scan_file) != scan["sha256"]:
                raise ValueError("original scan changed during the cut")
        except FAILURES as error:
            failure = fail(db, job, "cutout-failed", error)
            db.commit()
            raise failure from error
        place = folder.relative_to(workspace)
        db.execute("BEGIN IMMEDIATE")
        register(db, workspace, job, final=False)
        db.execute("UPDATE species SET state='cut' WHERE key=?", (bird["key"],))
        sources.event(
            db, bird["key"], "cut-completed", f"{place}; source revision {source['revision']}"
        )
        db.commit()
        try:
            tool(
                job,
                ADD_BIRD,
                str(image),
                "--style",
                settings["style"],
                "--key",
                bird["key"],
                "--source",
                source["record"]["source_key"],
                "--url",
                source["record"]["plate_url"],
                "--preview",
                str(folder / "preview.png"),
                "--dry-run",
                expect="preview.png",
            )
        except FAILURES as error:
            failure = fail(db, job, "preview-failed", error)
            register(db, workspace, job, final=True)
            db.commit()
            raise failure from error
        register(db, workspace, job, final=True)
        db.execute("UPDATE species SET state='needs-review' WHERE key=?", (bird["key"],))
        sources.event(
            db, bird["key"], "preview-built", f"{place}; native dry run, no quality approval"
        )
        return describe(db, workspace, bird["key"], revision) | {
            "species": bird["key"],
            "phase": "needs-review",
        }


def describe(db: sqlite3.Connection, workspace: Path, key: str, revision: int) -> dict:
    rows = db.execute(
        "SELECT kind, path, sha256 FROM artifacts WHERE species_key=? AND revision=? ORDER BY id",
        (key, revision),
    ).fetchall()
    intact = all(
        (workspace / row["path"]).is_file() and sources.sha256(workspace / row["path"]) == row[2]
        for row in rows
    )
    logs = [row["path"] for row in rows if row["kind"] == "log"]
    log = json.loads((workspace / logs[0]).read_text()) if logs and intact else {}
    return {
        "revision": revision,
        "path": str(Path(rows[0]["path"]).parent),
        "intact": intact,
        "artifacts": [dict(row) for row in rows],
    } | {
        name: log.get(name)
        for name in ("source_revision", "scan", "upstream", "notes", "warnings", "disclosures")
    }


def show(db: sqlite3.Connection, workspace: Path, identifier: str) -> dict | None:
    bird = store.find_species(db, identifier)
    if int(store.config(db)["schema_version"]) < 3:
        return None
    revisions = [
        row[0]
        for row in db.execute(
            "SELECT DISTINCT revision FROM artifacts WHERE species_key=? ORDER BY revision",
            (bird["key"],),
        )
    ]
    current = describe(db, workspace, bird["key"], revisions[-1]) if revisions else None
    return {"current": current, "revisions": revisions}


def revalidate(workspace: Path, identifier: str) -> dict:
    """Fresh native dry run on unchanged cut bytes, without rewriting old revisions."""
    from . import imports

    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_operations(db)
        bird = store.find_species(db, identifier)
        if not bird["active"] or bird["blocked_reason"] or bird["state"] != "needs-review":
            raise ValueError("revalidation requires an active unblocked cut at needs-review")
        source, _ = approved_scan(db, workspace, bird)
        sources.verify_evidence(workspace, source)
        current = show(db, workspace, bird["key"])["current"]
        if not current or not current["intact"] or current["source_revision"] != source["revision"]:
            raise ValueError("revalidation requires an intact current cut and approved source")
        root = Path(store.config(db)["upstream_path"])
        upstream.validate_checkout(root, workspace)
        baseline = imports.accounted_changes(db, root)
        native = upstream.inventory(root, [bird["scientific_name"]])
        if native["rows"][0]["key"] != bird["key"]:
            raise ValueError("upstream species key changed; rerun inventory")
        files = {item["kind"]: item for item in current["artifacts"]}
        image = files.get("final") or files["manual"]
        inputs = {
            "cut_revision": current["revision"],
            "upstream_revision": native["revision"],
            "source_fingerprint": source["fingerprint"],
            "image_sha256": image["sha256"],
        }
        now = datetime.now(UTC).isoformat()
        operation = db.execute(
            "INSERT INTO operations (species_key,kind,state,record_json,created_at,updated_at) VALUES (?,'revalidate','started','{}',?,?)",
            (bird["key"], now, now),
        ).lastrowid
        folder = workspace / f"work/revalidation/{bird['key']}/{operation:04d}"
        folder.mkdir(parents=True, exist_ok=False)
        record = {
            "inputs": inputs,
            "folder": str(folder.relative_to(workspace)),
            "commands": [],
            "files": {},
        }
        db.execute(
            "UPDATE operations SET record_json=? WHERE id=?", (json.dumps(record), operation)
        )
        db.commit()
        try:
            result = upstream.native(
                root,
                workspace,
                ADD_BIRD,
                str(workspace / image["path"]),
                "--style",
                store.config(db)["style"],
                "--key",
                bird["key"],
                "--source",
                source["record"]["source_key"],
                "--url",
                source["record"]["plate_url"],
                "--dry-run",
                "--preview",
                str(folder / "preview.png"),
            )
            record["commands"].append(result)
            destination = imports.DESTINATION.search(result["stdout"])
            expected = f"assets/artwork/{store.config(db)['style']}/birds/{bird['key']}.webp"
            if (
                result["returncode"]
                or not destination
                or destination[1] != expected
                or not (folder / "preview.png").is_file()
            ):
                raise ValueError(f"native revalidation did not file {expected}: {result['stderr']}")
            if (
                imports.accounted_changes(db, root) != baseline
                or upstream.run(["git", "rev-parse", "HEAD"], root) != native["revision"]
                or sources.sha256(workspace / image["path"]) != image["sha256"]
            ):
                raise ValueError("inputs changed during revalidation")
            record["files"] = {
                str(p.relative_to(workspace)): sources.sha256(p)
                for p in folder.iterdir()
                if p.is_file()
            }
        except Exception as error:
            record["error"] = str(error)
            db.execute(
                "UPDATE operations SET state='failed',record_json=? WHERE id=?",
                (json.dumps(record), operation),
            )
            db.commit()
            raise
        db.execute(
            "UPDATE operations SET state='complete',record_json=?,updated_at=? WHERE id=?",
            (json.dumps(record), datetime.now(UTC).isoformat(), operation),
        )
        sources.event(
            db,
            bird["key"],
            "cut-revalidated",
            f"Operation {operation}; unchanged cut, current native dry run; no quality approval",
        )
        return {"id": operation, "state": "complete", "record": record}
