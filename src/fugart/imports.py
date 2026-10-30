"""Provisional imports by upstream's own add_bird.py, journaled before it runs.

add_bird.py writes the plate and the manifest separately and numbers a new variant on
every call, so an import is never retried blind: the checkout is read first, and only
one that shows no trace of the attempt is imported again.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from . import cutout, sources, store, upstream

DESTINATION = re.compile(r"-> (\S+)\s+\((\d+)x(\d+)\)")
PENDING = "SELECT * FROM operations WHERE species_key=? AND kind='import' AND state='started'"


def head_json(root: Path, path: str) -> dict:
    if not upstream.run(["git", "ls-tree", "--name-only", "HEAD", "--", path], root):
        return {}
    return json.loads(upstream.run(["git", "show", f"HEAD:{path}"], root))


def accounted_changes(db: sqlite3.Connection, root: Path) -> dict[str, str]:
    """Verify whole files against HEAD plus completed journal entries, never just paths."""
    if db.execute("SELECT 1 FROM operations WHERE kind='replace' AND state='started'").fetchone():
        raise ValueError("pending replacement must be reconciled first")
    changed = upstream.changed_paths(root)
    if not changed:
        return {}
    expected = {}
    hashes = {}
    for row in db.execute(
        "SELECT kind, record_json FROM operations WHERE state='complete' ORDER BY id"
    ):
        made = json.loads(row["record_json"])
        if row["kind"] in ("import", "replace"):
            hashes[made["plate"]] = made["plate_sha256"]
            path, key, entry = made["manifest"], made["manifest_key"], made["manifest_entry"]
        elif row["kind"] == "box":
            path, key, entry = made["geometry"], made["geometry_key"], made["geometry_entry"]
        else:
            continue
        expected.setdefault(path, head_json(root, path))[key] = entry
        if row["kind"] == "replace":
            path = made["geometry"]
            expected.setdefault(path, head_json(root, path))[made["geometry_key"]] = made[
                "geometry_entry"
            ]
    try:
        for path, digest in hashes.items():
            if sources.sha256(root / path) != digest:
                raise ValueError(f"journaled plate changed: {path}")
        for path, entries in expected.items():
            if json.loads((root / path).read_text()) != entries:
                raise ValueError(f"unaccounted entries in {path}")
        if set(changed) - (hashes.keys() | expected.keys()):
            raise ValueError("paths outside completed journals")
        return {path: sources.sha256(root / path) for path in changed}
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(
            f"upstream checkout has uncommitted changes not accounted for: {error}"
        ) from error


def plan(db: sqlite3.Connection, workspace: Path, bird: dict) -> dict:
    """What this import is expected to write, checked against the reviewed cut."""
    if not bird["active"] or bird["blocked_reason"] or bird["state"] != "needs-review":
        raise ValueError("import requires an active, unblocked bird at needs-review")
    settings = store.config(db)
    root = Path(settings["upstream_path"])
    source, _ = cutout.approved_scan(db, workspace, bird)
    current = cutout.show(db, workspace, bird["key"])["current"]
    if not current or not current["intact"]:
        raise ValueError("import requires the current cut revision intact; recut it")
    if current["source_revision"] != source["revision"]:
        raise ValueError("the current cut was made from a superseded source revision; recut it")
    files = {item["kind"]: item for item in current["artifacts"]}
    image = files.get("final") or files["manual"]
    log = json.loads((workspace / files["log"]["path"]).read_text())
    head = upstream.run(["git", "rev-parse", "HEAD"], root)
    if head != log["upstream"]["revision"]:
        validation = db.execute(
            "SELECT record_json FROM operations WHERE species_key=? AND kind='revalidate' AND state='complete' ORDER BY id DESC LIMIT 1",
            (bird["key"],),
        ).fetchone()
        validation = json.loads(validation[0]) if validation else None
        expected = {
            "cut_revision": current["revision"],
            "upstream_revision": head,
            "source_fingerprint": source["fingerprint"],
            "image_sha256": image["sha256"],
        }
        if (
            not validation
            or validation["inputs"] != expected
            or not validation["files"]
            or not all(
                sources.sha256(workspace / path) == digest
                for path, digest in validation["files"].items()
            )
        ):
            raise ValueError(
                "upstream has moved since this cut's dry run; run revalidate (or recut) first"
            )
        log = validation
    baseline = accounted_changes(db, root)
    style = f"assets/artwork/{settings['style']}"
    plate = f"{style}/birds/{bird['key']}.webp"
    filed = DESTINATION.search(log["commands"][-1]["stdout"])
    if not filed or filed[1] != plate:
        raise ValueError(f"the reviewed dry run did not file this bird as {plate}; recut it")
    entry = {"source": source["record"]["source_key"], "url": source["record"]["plate_url"]}
    return {
        "cut_revision": current["revision"],
        "image": {"path": image["path"], "sha256": image["sha256"]},
        "source_revision": source["revision"],
        "upstream_revision": head,
        "plate": plate,
        "size": [int(filed[2]), int(filed[3])],
        "manifest": f"{style}/manifest.json",
        "manifest_key": f"birds/{bird['key']}.webp",
        "manifest_entry": entry,
        "changed_before": baseline,
        "manifest_before": json.loads((root / f"{style}/manifest.json").read_text()),
        "args": [
            cutout.ADD_BIRD,
            str(workspace / image["path"]),
            "--style",
            settings["style"],
            "--key",
            bird["key"],
            "--source",
            entry["source"],
            "--url",
            entry["url"],
            "--no-detect",
            "--no-box",
        ],
        "commands": [],
    }


def begin(db: sqlite3.Connection, workspace: Path, bird: dict) -> tuple[int, dict]:
    """Journal the planned import; the caller commits before anything touches the checkout."""
    record = plan(db, workspace, bird)
    now = datetime.now(UTC).isoformat()
    cursor = db.execute(
        """INSERT INTO operations (species_key, kind, state, record_json, created_at, updated_at)
        VALUES (?, 'import', 'started', ?, ?, ?)""",
        (bird["key"], json.dumps(record), now, now),
    )
    return cursor.lastrowid, record


def observe(root: Path, record: dict) -> str:
    """What the checkout shows of an attempt: "untouched", "complete", or what conflicts."""
    if upstream.run(["git", "rev-parse", "HEAD"], root) != record["upstream_revision"]:
        return "upstream HEAD moved since the import began"
    changed = upstream.changed_paths(root)
    baseline = record.get("changed_before", {})
    try:
        if changed == sorted(baseline) and all(
            sources.sha256(root / path) == digest for path, digest in baseline.items()
        ):
            return "untouched"
        for path, digest in baseline.items():
            if path != record["manifest"] and sources.sha256(root / path) != digest:
                return f"previously journaled file changed during import: {path}"
    except OSError as error:
        return f"unreadable previous import: {error}"
    if changed != sorted(set(baseline) | {record["plate"], record["manifest"]}):
        return f"expected a new plate and its manifest entry, found: {', '.join(changed)}"
    before = record.get("manifest_before")
    if before is None:
        before = head_json(root, record["manifest"])
    try:
        after = json.loads((root / record["manifest"]).read_text())
        with Image.open(root / record["plate"]) as plate:
            plate.load()
            size = list(plate.size)
    except (OSError, ValueError) as error:
        return f"unreadable import: {error}"
    if after != before | {record["manifest_key"]: record["manifest_entry"]}:
        return "the manifest differs by more than this bird's entry"
    if size != record["size"]:
        return f"the plate is {size[0]}x{size[1]}, not the reviewed {record['size']}"
    return "complete"


def settle(db: sqlite3.Connection, root: Path, key: str, operation: int, record: dict):
    """Close the journal from what is in the checkout; None when the attempt left no trace."""
    seen = observe(root, record)
    record["outcome"] = seen
    state = {"untouched": "failed", "complete": "complete"}.get(seen, "started")
    if state == "complete":
        record["plate_sha256"] = sources.sha256(root / record["plate"])
        record["manifest_sha256"] = sources.sha256(root / record["manifest"])
    db.execute(
        "UPDATE operations SET state=?, record_json=?, updated_at=? WHERE id=?",
        (state, json.dumps(record), datetime.now(UTC).isoformat(), operation),
    )
    if state == "failed":
        sources.event(db, key, "import-failed", "nothing was written upstream")
        db.commit()
        return None
    if state == "started":
        sources.event(db, key, "import-conflict", seen)
        db.commit()
        raise ValueError(
            f"{seen}; repair the checkout by hand, then rerun import to reconcile it "
            "(add_bird.py is not run again while this attempt is unresolved)"
        )
    db.execute("UPDATE species SET state='imported' WHERE key=?", (key,))
    sources.event(
        db, key, "imported", f"{record['plate']}; provisional until boxed and quality-approved"
    )
    return {
        "species": key,
        "phase": "imported",
        "operation": operation,
        "plate": record["plate"],
        "plate_sha256": record["plate_sha256"],
    }


def run(workspace: Path, identifier: str) -> dict:
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_operations(db)
        bird = store.find_species(db, identifier)
        root = Path(store.config(db)["upstream_path"])
        upstream.validate_checkout(root, workspace)
        if pending := db.execute(PENDING, (bird["key"],)).fetchone():
            record = json.loads(pending["record_json"])
            if result := settle(db, root, bird["key"], pending["id"], record):
                return result
        operation, record = begin(db, workspace, bird)
        db.commit()
        entry = upstream.native(root, workspace, *record["args"])
        record["commands"].append(entry)
        if result := settle(db, root, bird["key"], operation, record):
            return result
        raise ValueError(f"add_bird.py wrote nothing: {entry['stderr'].strip()[-800:]}")


def show(db: sqlite3.Connection, identifier: str) -> dict | None:
    if int(store.config(db)["schema_version"]) < 4:
        return None
    row = db.execute(
        "SELECT * FROM operations WHERE species_key=? AND kind IN ('import','replace') ORDER BY id DESC LIMIT 1",
        (store.find_species(db, identifier)["key"],),
    ).fetchone()
    return row and dict(row) | {"record": json.loads(row["record_json"])}
