"""Native final review evidence and explicit, fingerprint-bound human approvals."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from . import boxing, cutout, fetch, imports, sources, store, upstream


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(db: sqlite3.Connection, workspace: Path, bird: dict) -> dict:
    root = Path(store.config(db)["upstream_path"])
    imported = imports.show(db, bird["key"])
    boxed = boxing.show(db, bird["key"])
    source = sources.current(db, bird["key"])
    cut = cutout.show(db, workspace, bird["key"])["current"]
    if not imported or imported["state"] != "complete" or not boxed or boxed["state"] != "complete":
        raise ValueError("review requires completed import and box journals")
    if not source or not source["approval"] or not cut or not cut["intact"]:
        raise ValueError("review requires intact approved source and cut")
    sources.verify_identity(bird, source)
    sources.verify_evidence(workspace, source)
    downloaded = db.execute(
        "SELECT record_json FROM downloads WHERE source_id=?", (source["id"],)
    ).fetchone()
    if not downloaded:
        raise ValueError("review requires original scan evidence")
    scan = json.loads(downloaded[0])
    if sources.sha256(fetch.scan_path(workspace, scan["path"])) != scan["sha256"]:
        raise ValueError("original scan checksum mismatch")
    made, box = imported["record"], boxed["record"]
    if (
        cut["revision"] != made["cut_revision"]
        or source["revision"] != made["source_revision"]
        or source["approval"]["fingerprint"] != source["fingerprint"]
        or box.get("import_operation") != imported["id"]
        or box.get("cut_revision") != cut["revision"]
        or box.get("plate_sha256") != made["plate_sha256"]
    ):
        raise ValueError("import source or cut changed")
    entry = json.loads((root / made["manifest"]).read_text()).get(made["manifest_key"])
    geometry = json.loads((root / box["geometry"]).read_text()).get(box["geometry_key"])
    plate_sha = sources.sha256(root / made["plate"])
    if (
        plate_sha != made["plate_sha256"]
        or entry != made["manifest_entry"]
        or geometry != box["geometry_entry"]
    ):
        raise ValueError("imported artwork or saved box changed")
    # This also rejects unrelated unjournaled edits before generating new evidence.
    imports.accounted_changes(db, root)
    return {
        "upstream_revision": upstream.run(["git", "rev-parse", "HEAD"], root),
        "source_fingerprint": source["fingerprint"],
        "source_approval": source["approval"],
        "cut_revision": cut["revision"],
        "import_operation": imported["id"],
        "box_operation": boxed["id"],
        "plate": made["plate"],
        "plate_sha256": plate_sha,
        "manifest_entry": entry,
        "geometry_entry": geometry,
        "box_method": box.get("box_method", "human editor"),
    }


def valid(db: sqlite3.Connection, workspace: Path, bird: dict, record: dict) -> bool:
    try:
        return (
            bird["active"]
            and not bird["blocked_reason"]
            and snapshot(db, workspace, bird) == record["inputs"]
            and all(
                sources.sha256(workspace / path) == digest
                for path, digest in record["files"].items()
            )
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def latest(db: sqlite3.Connection, key: str):
    if int(store.config(db)["schema_version"]) < 4:
        return None
    row = db.execute(
        "SELECT * FROM operations WHERE species_key=? AND kind='review-build' ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    return row and dict(row) | {"record": json.loads(row["record_json"])}


def show(db: sqlite3.Connection, workspace: Path, identifier: str):
    bird = store.find_species(db, identifier)
    row = latest(db, bird["key"])
    if not row:
        return None
    approval = db.execute(
        "SELECT record_json FROM operations WHERE species_key=? AND kind='quality-review' AND state='complete' ORDER BY id DESC LIMIT 1",
        (bird["key"],),
    ).fetchone()
    approval = json.loads(approval[0]) if approval else None
    intact = row["state"] == "complete" and valid(db, workspace, bird, row["record"])
    return row | {
        "valid": bool(intact),
        "approval": approval,
        "approved": bool(
            intact
            and approval
            and approval["review"] == row["id"]
            and approval["fingerprint"] == fingerprint(row["record"])
        ),
    }


def effective(db: sqlite3.Connection, bird: dict) -> dict:
    """An old approval remains audit history, but cannot present as current approval."""
    if bird["state"] == "approved":
        database = db.execute("PRAGMA database_list").fetchone()[2]
        current = show(db, Path(database).parent.parent, bird["key"])
        if not current or not current["approved"]:
            return bird | {"state": "boxed"}
    return bird


def build(workspace: Path, identifier: str) -> dict:
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_operations(db)
        bird = store.find_species(db, identifier)
        if (
            not bird["active"]
            or bird["blocked_reason"]
            or bird["state"] not in ("boxed", "approved")
        ):
            raise ValueError("review requires an active unblocked boxed bird")
        before = snapshot(db, workspace, bird)
        root = Path(store.config(db)["upstream_path"])
        cut = workspace / f"work/cuts/{bird['key']}/r{before['cut_revision']:03d}"
        if not (cut / "crop.png").is_file() or not (cut / "cut.png").is_file():
            raise ValueError(
                "native review requires matching crop.png and cut.png; manual cuts need comparison evidence"
            )
        now = datetime.now(UTC).isoformat()
        operation = db.execute(
            "INSERT INTO operations (species_key, kind, state, record_json, created_at, updated_at) VALUES (?, 'review-build', 'started', '{}', ?, ?)",
            (bird["key"], now, now),
        ).lastrowid
        folder = workspace / f"work/review/{bird['key']}/review-{operation:04d}"
        record = {
            "inputs": before,
            "folder": str(folder.relative_to(workspace)),
            "commands": [],
            "files": {},
        }
        db.execute(
            "UPDATE operations SET record_json=? WHERE id=?", (json.dumps(record), operation)
        )
        db.commit()
        try:
            folder.mkdir(parents=True, exist_ok=False)
            (folder / "plates.json").write_text(
                json.dumps(
                    [
                        {
                            "id": bird["key"],
                            "name": bird["common_name"],
                            "pr": "",
                            "plate": str(root / before["plate"]),
                            "scan": str(cut / "crop.png"),
                            "cut": str(cut / "cut.png"),
                        }
                    ],
                    indent=2,
                )
            )
            for script, args in [
                ("tools/platereview/build.py", [str(folder)]),
                ("tools/preview_plates.py", [str(folder / "preview"), before["plate"]]),
            ]:
                result = upstream.native(root, workspace, script, *args)
                record["commands"].append(result)
                if result["returncode"]:
                    raise ValueError(f"native review failed: {result.get('stderr', '')}")
            for required in ("birds.js", "index.html", "preview/web.png", "preview/panel.png"):
                if not (folder / required).is_file():
                    raise ValueError(f"native review missing {required}")
            record["files"] = {
                str(p.relative_to(workspace)): sources.sha256(p)
                for p in sorted(folder.rglob("*"))
                if p.is_file()
            }
            if snapshot(db, workspace, bird) != before:
                raise ValueError("review inputs changed during build")
        except Exception as error:
            record["error"] = str(error)
            db.execute(
                "UPDATE operations SET state='failed', record_json=?, updated_at=? WHERE id=?",
                (json.dumps(record), datetime.now(UTC).isoformat(), operation),
            )
            db.commit()
            raise
        db.execute(
            "UPDATE operations SET state='complete', record_json=?, updated_at=? WHERE id=?",
            (json.dumps(record), datetime.now(UTC).isoformat(), operation),
        )
        sources.event(
            db, bird["key"], "review-built", f"Operation {operation}; no quality approval"
        )
        return {"id": operation, "state": "complete", "record": record}


def approve(workspace: Path, identifier: str, operation: int, reviewer: str, notes: str):
    if not reviewer.strip() or not notes.strip():
        raise ValueError("reviewer and notes must be nonempty")
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_operations(db)
        bird = store.find_species(db, identifier)
        row = latest(db, bird["key"])
        if not row or row["id"] != operation or row["state"] != "complete":
            raise ValueError("approval must name the latest complete review operation")
        if not valid(db, workspace, bird, row["record"]):
            raise ValueError("stale review evidence; rebuild and review again")
        record = {
            "review": operation,
            "fingerprint": fingerprint(row["record"]),
            "reviewer": reviewer.strip(),
            "notes": notes.strip(),
        }
        now = datetime.now(UTC).isoformat()
        db.execute(
            "INSERT INTO operations (species_key, kind, state, record_json, created_at, updated_at) VALUES (?, 'quality-review', 'complete', ?, ?, ?)",
            (bird["key"], json.dumps(record), now, now),
        )
        db.execute("UPDATE species SET state='approved' WHERE key=?", (bird["key"],))
        sources.event(db, bird["key"], "quality-approved", json.dumps(record))
        return {"species": bird["key"], "phase": "approved", **record}
