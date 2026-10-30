"""Journal a human sitting with upstream's autosaving bird box editor."""

import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import cutout, imports, sources, store, upstream

PENDING = "SELECT * FROM operations WHERE species_key=? AND kind='box' AND state='started'"


def measure(root: Path, workspace: Path, plate: str) -> list[int]:
    # Boxes are measured against upstream's trimmed image, not the file dimensions.
    result = upstream.native(
        root,
        workspace,
        "-c",
        "import json, sys; from pathlib import Path; from tools.bird_box import _size; "
        "print(json.dumps(_size(Path(sys.argv[1]))))",
        plate,
    )
    if result["returncode"]:
        raise ValueError(f"cannot measure imported plate: {result['stderr']}")
    return json.loads(result["stdout"])


def launch(root: Path, workspace: Path, record: dict) -> dict:
    argv = ["uv", "run", "--locked", "python", "-B", *record["args"]]
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"} | {
        "UV_PROJECT_ENVIRONMENT": str(workspace / ".fugart/upstream-env"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "BROWSER": "true",
    }
    # Fail closed if this Python would choose a real browser despite BROWSER=true.
    probe = subprocess.run(
        [*argv[:5], "-c", "import webbrowser; assert webbrowser.get().name == 'true'"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if probe.returncode:
        raise ValueError(f"cannot suppress external browser: {probe.stderr.strip()}")
    started = datetime.now(UTC).isoformat()
    print(
        f"Box editor: {record['url']} - autosaves; Ctrl-C ends the session.",
        file=sys.stderr,
        flush=True,
    )
    # A sitting has no timeout. Temporary files avoid pipe backpressure while the
    # editor runs; the durable row already exists if this process is killed.
    with tempfile.TemporaryFile(mode="w+") as out, tempfile.TemporaryFile(mode="w+") as err:
        process = subprocess.Popen(argv, cwd=root, env=env, stdout=out, stderr=err)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            code = process.wait(timeout=15)
        out.seek(0)
        err.seek(0)
        return {
            "argv": argv,
            "started_at": started,
            "returncode": code,
            "stdout": out.read(),
            "stderr": err.read(),
            "BROWSER": "true",
        }


def plan(db: sqlite3.Connection, workspace: Path, bird: dict, port: int) -> dict:
    imported = imports.show(db, bird["key"])
    # A replaced cut carries a box reprojected from the old one, which can be wrong for the
    # new plate (and is only a placeholder when the old cut was), so it can be boxed again.
    rebox = (
        bird["state"] in ("boxed", "approved")
        and imported
        and imported["record"].get("mode") == "replace"
    )
    if not bird["active"] or bird["blocked_reason"] or (bird["state"] != "imported" and not rebox):
        raise ValueError("box requires an active, unblocked imported or replaced bird")
    if not imported or imported["state"] != "complete":
        raise ValueError("box requires a completed import journal")
    made = imported["record"]
    root = Path(store.config(db)["upstream_path"])
    source = sources.current(db, bird["key"])
    current = cutout.show(db, workspace, bird["key"])["current"]
    if (
        not source
        or not source["approval"]
        or source["approval"]["fingerprint"] != source["fingerprint"]
        or source["revision"] != made["source_revision"]
        or not current
        or not current["intact"]
        or current["revision"] != made["cut_revision"]
    ):
        raise ValueError("import's approved source and cut must remain intact")
    if sources.sha256(root / made["plate"]) != made["plate_sha256"]:
        raise ValueError("imported plate checksum mismatch")
    manifest = json.loads((root / made["manifest"]).read_text())
    if manifest.get(made["manifest_key"]) != made["manifest_entry"]:
        raise ValueError("imported manifest entry mismatch")
    changed = imports.accounted_changes(db, root)
    geometry = str(Path(made["manifest"]).with_name("geometry.json"))
    path = root / geometry
    previous = json.loads(path.read_text()) if path.exists() else {}
    last = show(db, bird["key"])
    if made["manifest_key"] in previous and not (
        rebox
        and last
        and last["state"] == "complete"
        and previous[made["manifest_key"]] == last["record"].get("geometry_entry")
    ):
        raise ValueError(
            "imported plate already has a box outside a box journal; reconcile by hand"
        )
    return {
        "import_operation": imported["id"],
        "cut_revision": made["cut_revision"],
        "source_revision": made["source_revision"],
        "upstream_revision": upstream.run(["git", "rev-parse", "HEAD"], root),
        "plate": made["plate"],
        "plate_sha256": made["plate_sha256"],
        "manifest": made["manifest"],
        "manifest_sha256": sources.sha256(root / made["manifest"]),
        "geometry": geometry,
        "geometry_key": made["manifest_key"],
        "geometry_before": previous,
        "cut": measure(root, workspace, made["plate"]),
        "changed_before": changed,
        "args": ["tools/bird_box.py", made["plate"], "--port", str(port)],
        "url": f"http://127.0.0.1:{port}/",
        "commands": [],
    }


def observe(root: Path, record: dict) -> str:
    if upstream.run(["git", "rev-parse", "HEAD"], root) != record["upstream_revision"]:
        return "upstream HEAD moved during boxing"
    for name in ("plate", "manifest"):
        path = root / record[name]
        if not path.is_file() or sources.sha256(path) != record[f"{name}_sha256"]:
            return f"{name} changed during boxing"
    for name, digest in record["changed_before"].items():
        if name == record["geometry"]:
            continue
        path = root / name
        if not path.is_file() or sources.sha256(path) != digest:
            return f"previously journaled file changed during boxing: {name}"
    if set(upstream.changed_paths(root)) - set(record["changed_before"]) - {record["geometry"]}:
        return "unrelated checkout changes during boxing"
    try:
        path = root / record["geometry"]
        after = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(after, dict):
            return "geometry must be an object"
        if after == record["geometry_before"]:
            return "untouched"
        entry = after.get(record["geometry_key"])
        if after != record["geometry_before"] | {record["geometry_key"]: entry}:
            return "geometry contains changes to other plates"
        box = entry["box"]
        if (
            entry["cut"] != record["cut"]
            or len(box) != 4
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
            or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)
        ):
            return "invalid or stale bird box"
    except (OSError, ValueError, TypeError, KeyError):
        return "unreadable bird box"
    return "complete"


def settle(db: sqlite3.Connection, root: Path, key: str, operation: int, record: dict):
    seen = observe(root, record)
    record["outcome"] = seen
    state = {"untouched": "failed", "complete": "complete"}.get(seen, "started")
    if state == "complete":
        record["geometry_entry"] = json.loads((root / record["geometry"]).read_text())[
            record["geometry_key"]
        ]
        record["geometry_sha256"] = sources.sha256(root / record["geometry"])
    db.execute(
        "UPDATE operations SET state=?, record_json=?, updated_at=? WHERE id=?",
        (state, json.dumps(record), datetime.now(UTC).isoformat(), operation),
    )
    if state != "complete":
        sources.event(db, key, f"box-{'failed' if state == 'failed' else 'conflict'}", seen)
        db.commit()
        if state == "started":
            raise ValueError(
                f"{seen}; repair the checkout, then rerun box to reconcile without relaunching"
            )
        return None
    db.execute("UPDATE species SET state='boxed' WHERE key=?", (key,))
    sources.event(db, key, "boxed", f"{record['geometry']}; human quality approval still pending")
    return {
        "species": key,
        "phase": "boxed",
        "operation": operation,
        "geometry": record["geometry"],
        "geometry_entry": record["geometry_entry"],
    }


def run(workspace: Path, identifier: str, port: int = 8081) -> dict:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_operations(db)
        bird = store.find_species(db, identifier)
        root = Path(store.config(db)["upstream_path"])
        upstream.validate_checkout(root, workspace)
        if pending := db.execute(PENDING, (bird["key"],)).fetchone():
            record = json.loads(pending["record_json"])
            if result := settle(db, root, bird["key"], pending["id"], record):
                return result
        record = plan(db, workspace, bird, port)
        now = datetime.now(UTC).isoformat()
        operation = db.execute(
            "INSERT INTO operations (species_key, kind, state, record_json, created_at, updated_at) "
            "VALUES (?, 'box', 'started', ?, ?, ?)",
            (bird["key"], json.dumps(record), now, now),
        ).lastrowid
        db.commit()
        record["commands"].append(launch(root, workspace, record))
        if result := settle(db, root, bird["key"], operation, record):
            return result
        raise ValueError("no box was saved; bird keeps its previous phase")


def show(db: sqlite3.Connection, identifier: str) -> dict | None:
    if int(store.config(db)["schema_version"]) < 4:
        return None
    row = db.execute(
        # a failed (unsaved) session leaves the last saved box in force
        "SELECT * FROM operations WHERE species_key=? AND kind IN ('box','replace') "
        "AND state != 'failed' ORDER BY id DESC LIMIT 1",
        (store.find_species(db, identifier)["key"],),
    ).fetchone()
    return row and dict(row) | {"record": json.loads(row["record_json"])}


def propose(
    workspace: Path,
    identifier: str,
    box: list[float],
    notes: str,
    method: str = "agent-proposed; human sign-off pending",
) -> dict:
    """Save a box without the native editor: an agent's proposal, or one drawn in `fugart ui`."""
    from .revisions import atomic_write

    if (
        not notes.strip()
        or len(box) != 4
        or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
        or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)
    ):
        raise ValueError("proposed box needs finite ordered normalized coordinates and notes")
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_operations(db)
        bird = store.find_species(db, identifier)
        root = Path(store.config(db)["upstream_path"])
        upstream.validate_checkout(root, workspace)
        pending = db.execute(PENDING, (bird["key"],)).fetchone()
        if pending:
            result = settle(
                db, root, bird["key"], pending["id"], json.loads(pending["record_json"])
            )
            if result:
                return result
        record = plan(db, workspace, bird, 8081)
        record.update(
            box_method=method,
            notes=notes.strip(),
            proposed_box=box,
        )
        now = datetime.now(UTC).isoformat()
        operation = db.execute(
            "INSERT INTO operations (species_key,kind,state,record_json,created_at,updated_at) VALUES (?,'box','started',?,?,?)",
            (bird["key"], json.dumps(record), now, now),
        ).lastrowid
        db.commit()
        after = record["geometry_before"] | {
            record["geometry_key"]: {"box": box, "cut": record["cut"]}
        }
        # Recheck the complete baseline before writing this bird's geometry only.
        if imports.accounted_changes(db, root) != record["changed_before"]:
            raise ValueError("checkout changed before proposed box save")
        atomic_write(
            root / record["geometry"], json.dumps(after, indent=1, sort_keys=True).encode() + b"\n"
        )
        result = settle(db, root, bird["key"], operation, record)
        if not result:
            raise ValueError("proposed box was not saved")
        return result
