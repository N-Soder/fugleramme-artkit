"""Small transactional work-state store; never writes into the upstream tree."""

import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from . import upstream

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE inventory_runs (
    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
    checklist_json TEXT NOT NULL, checklist_sha256 TEXT NOT NULL,
    upstream_revision TEXT NOT NULL, upstream_dirty INTEGER NOT NULL,
    upstream_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
    total INTEGER NOT NULL, covered INTEGER NOT NULL, missing INTEGER NOT NULL
);
CREATE TABLE species (
    key TEXT PRIMARY KEY, scientific_name TEXT NOT NULL, common_name TEXT NOT NULL,
    label_names TEXT NOT NULL, records INTEGER NOT NULL, artwork TEXT NOT NULL,
    state TEXT CHECK(state IN ('missing','researching','sourced','downloaded','cut',
      'needs-review','imported','boxed','approved','ready-pr','submitted','merged')),
    blocked_reason TEXT CHECK(blocked_reason IN ('source-unavailable',
      'taxonomy-ambiguity','licensing-uncertainty','artwork-quality')),
    notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
    inventory_run INTEGER NOT NULL REFERENCES inventory_runs(id)
);
CREATE TABLE batches (
    id TEXT PRIMARY KEY, upstream_revision TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE batch_members (
    batch_id TEXT NOT NULL REFERENCES batches(id),
    species_key TEXT NOT NULL UNIQUE REFERENCES species(key),
    PRIMARY KEY(batch_id, species_key)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, species_key TEXT REFERENCES species(key),
    batch_id TEXT REFERENCES batches(id), action TEXT NOT NULL,
    created_at TEXT NOT NULL, notes TEXT NOT NULL
);
"""

SOURCE_SCHEMA = (
    """CREATE TABLE sources (
        id INTEGER PRIMARY KEY, species_key TEXT NOT NULL REFERENCES species(key),
        revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, record_json TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(species_key, revision))""",
    """CREATE TABLE source_reviews (
        id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL UNIQUE REFERENCES sources(id),
        reviewer TEXT NOT NULL, notes TEXT NOT NULL, fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE downloads (
        source_id INTEGER PRIMARY KEY REFERENCES sources(id), record_json TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE fetch_jobs (
        source_id INTEGER PRIMARY KEY REFERENCES sources(id), record_json TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
)
ARTIFACT_SCHEMA = """CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY, species_key TEXT NOT NULL REFERENCES species(key),
    revision INTEGER NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES sources(id),
    record_json TEXT NOT NULL, created_at TEXT NOT NULL)"""
OPERATION_SCHEMA = """CREATE TABLE operations (
    id INTEGER PRIMARY KEY, species_key TEXT NOT NULL REFERENCES species(key),
    kind TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('started','complete','failed')),
    record_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
# The statements that take a workspace from each schema version to the next.
MIGRATIONS = {"1": SOURCE_SCHEMA, "2": (ARTIFACT_SCHEMA,), "3": (OPERATION_SCHEMA,)}
LATEST = "4"
BLOCK_REASONS = (
    "source-unavailable",
    "taxonomy-ambiguity",
    "licensing-uncertainty",
    "artwork-quality",
)


def db_path(workspace: Path) -> Path:
    return workspace / ".fugart" / "state.sqlite3"


@contextmanager
def connect(workspace: Path, *, readonly: bool = False):
    path = db_path(workspace)
    if not path.is_file():
        raise ValueError("workspace is not initialized; run fugart init --upstream PATH")
    db = sqlite3.connect(f"{path.as_uri()}?mode={'ro' if readonly else 'rw'}", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not version or version[0] not in (*MIGRATIONS, LATEST):
            raise ValueError(
                "unsupported state schema; keep this database and use a compatible CLI"
            )
        with db:
            yield db
    finally:
        db.close()


def config(db: sqlite3.Connection) -> dict[str, str]:
    return dict(db.execute("SELECT key, value FROM meta"))


def initialize(workspace: Path, root: Path) -> dict:
    upstream.validate_checkout(root, workspace)
    path = db_path(workspace)
    if path.exists():
        with connect(workspace, readonly=True) as db:
            settings = config(db)
            if settings["upstream_path"] != str(root):
                raise ValueError(f"workspace already bound to {settings['upstream_path']}")
        return {"created": False, "database": str(path), "upstream": str(root), "style": "classic"}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(prefix="init-", suffix=".sqlite3", dir=path.parent)
    os.close(fd)
    try:
        with sqlite3.connect(scratch) as db:
            db.executescript(SCHEMA)
            for statements in MIGRATIONS.values():
                for statement in statements:
                    db.execute(statement)
            db.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [("schema_version", LATEST), ("upstream_path", str(root)), ("style", "classic")],
            )
        db.close()
        # Link atomically without replacing a database created by another invocation.
        os.link(scratch, path)
    finally:
        Path(scratch).unlink()
    (workspace / "work").mkdir(exist_ok=True)
    return {"created": True, "database": str(path), "upstream": str(root), "style": "classic"}


@contextmanager
def mutation_lock(workspace: Path):
    if not db_path(workspace).is_file():
        raise ValueError("workspace is not initialized; run fugart init --upstream PATH")
    with (workspace / ".fugart/mutation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another operation holds the workspace lock") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def require_sources(db: sqlite3.Connection) -> None:
    if config(db)["schema_version"] == "1":
        raise ValueError("source workflow needs schema 2; run fugart migrate (creates a backup)")


def require_artifacts(db: sqlite3.Connection) -> None:
    if int(config(db)["schema_version"]) < 3:
        raise ValueError("cut revisions need schema 3; run fugart migrate (creates a backup)")


def require_operations(db: sqlite3.Connection) -> None:
    if int(config(db)["schema_version"]) < 4:
        raise ValueError("imports need schema 4; run fugart migrate (creates a backup)")


def migrate(workspace: Path) -> dict:
    with mutation_lock(workspace), connect(workspace) as db:
        version = config(db)["schema_version"]
        if version == LATEST:
            return {"schema_version": int(LATEST), "changed": False, "backup": None}
        backup_dir = workspace / ".fugart/backups"
        backup_dir.mkdir(exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=f"schema-{version}-", suffix=".sqlite3", dir=backup_dir)
        os.close(fd)
        with sqlite3.connect(path) as backup:
            db.backup(backup)
        backup.close()
        db.execute("BEGIN IMMEDIATE")
        for step in sorted(step for step in MIGRATIONS if step >= version):
            for statement in MIGRATIONS[step]:
                db.execute(statement)
        db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (LATEST,))
        return {"schema_version": int(LATEST), "changed": True, "backup": path}


def read_checklist(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("checklist must use schema_version 1")
    if data.get("complete") is not True:
        raise ValueError("incomplete checklist; refusing a partial inventory")
    for field in ("source_url", "scope", "retrieved_at"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"checklist needs {field}")
    source = urlparse(data["source_url"])
    if source.scheme not in ("http", "https") or not source.netloc:
        raise ValueError("checklist source_url must be an HTTP(S) URL")
    retrieved = datetime.fromisoformat(data["retrieved_at"])
    if retrieved.tzinfo is None:
        raise ValueError("checklist retrieved_at needs a timezone")
    rows = data.get("species")
    if not isinstance(rows, list) or not rows:
        raise ValueError("checklist needs a nonempty species list")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("checklist species must be records")
        name = row.get("scientific_name")
        if not isinstance(name, str) or not name.strip() or name in seen:
            raise ValueError("checklist species need unique nonempty scientific_name values")
        seen.add(name)
        records = row.get("records", 0)
        if type(records) is not int or records < 0:
            raise ValueError("checklist records must be nonnegative integers")
    return data, hashlib.sha256(raw).hexdigest()


def refresh(workspace: Path, path: Path) -> dict:
    checklist, checklist_sha = read_checklist(path)
    with mutation_lock(workspace), connect(workspace) as db:
        settings = config(db)
        root = Path(settings["upstream_path"])
        upstream.validate_checkout(root, workspace)
        snapshot = upstream.inventory(
            root, [row["scientific_name"] for row in checklist["species"]]
        )
        birds = {}
        for requested, native in zip(checklist["species"], snapshot["rows"], strict=True):
            row = native["row"]
            key = native["key"]
            bird = birds.setdefault(
                key,
                {
                    "key": key,
                    "scientific_name": row["name"],
                    "common_name": row["common"],
                    "label_names": native["label_names"],
                    "artwork": [
                        p for p in row["plates"] if p.startswith(f"{settings['style']}/birds/")
                    ],
                    "records": 0,
                },
            )
            bird["records"] += requested.get("records", 0)
        total = len(birds)
        covered = sum(bool(bird["artwork"]) for bird in birds.values())
        fingerprint = hashlib.sha256(
            json.dumps(
                [checklist_sha, snapshot["revision"], snapshot["dirty"], snapshot["fingerprint"]]
            ).encode()
        ).hexdigest()
        db.execute("BEGIN IMMEDIATE")
        found = db.execute(
            "SELECT * FROM inventory_runs WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        latest = config(db).get("last_inventory_run")
        if found and latest == str(found["id"]):
            return inventory_result(found, changed=False)
        now = datetime.now(UTC).isoformat()
        if found:
            run_id = found["id"]
        else:
            cursor = db.execute(
                """INSERT INTO inventory_runs (fingerprint, checklist_json, checklist_sha256,
                upstream_revision, upstream_dirty, upstream_fingerprint, created_at,
                total, covered, missing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fingerprint,
                    json.dumps(checklist),
                    checklist_sha,
                    snapshot["revision"],
                    snapshot["dirty"],
                    snapshot["fingerprint"],
                    now,
                    total,
                    covered,
                    total - covered,
                ),
            )
            run_id = cursor.lastrowid
        db.execute("UPDATE species SET active=0")
        for bird in birds.values():
            exists = db.execute("SELECT 1 FROM species WHERE key=?", (bird["key"],)).fetchone()
            state = None if bird["artwork"] else "missing"
            db.execute(
                """INSERT INTO species (key, scientific_name, common_name, label_names,
                records, artwork, state, inventory_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET scientific_name=excluded.scientific_name,
                common_name=excluded.common_name, label_names=excluded.label_names,
                records=excluded.records, artwork=excluded.artwork, active=1,
                inventory_run=excluded.inventory_run,
                state=COALESCE(species.state, excluded.state)""",
                (
                    bird["key"],
                    bird["scientific_name"],
                    bird["common_name"],
                    json.dumps(bird["label_names"]),
                    bird["records"],
                    json.dumps(bird["artwork"]),
                    state,
                    run_id,
                ),
            )
            if not exists and state:
                db.execute(
                    "INSERT INTO events (species_key, action, created_at, notes) VALUES (?, ?, ?, ?)",
                    (bird["key"], "backlog-created", now, f"inventory {run_id}"),
                )
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_inventory_run', ?)", (str(run_id),))
        row = db.execute("SELECT * FROM inventory_runs WHERE id=?", (run_id,)).fetchone()
        return inventory_result(row, changed=True)


def inventory_result(row: sqlite3.Row, *, changed: bool) -> dict:
    return {
        name: row[name]
        for name in (
            "id",
            "total",
            "covered",
            "missing",
            "upstream_revision",
            "checklist_sha256",
            "created_at",
        )
    } | {
        "upstream_dirty": bool(row["upstream_dirty"]),
        "region": json.loads(row["checklist_json"]).get("region"),
        "changed": changed,
    }


def decode_species(row: sqlite3.Row) -> dict:
    bird = dict(row)
    bird["label_names"] = json.loads(bird["label_names"])
    bird["artwork"] = json.loads(bird["artwork"])
    bird["active"] = bool(bird["active"])
    return bird


def find_species(db: sqlite3.Connection, identifier: str) -> dict:
    query = identifier.strip().casefold()
    matches = []
    for row in db.execute("SELECT * FROM species ORDER BY key"):
        bird = decode_species(row)
        names = [bird["key"], bird["scientific_name"], bird["common_name"], *bird["label_names"]]
        if query in {name.casefold() for name in names}:
            matches.append(bird)
    if not matches:
        raise ValueError(f"unknown species: {identifier}; use fugart list")
    if len(matches) > 1:
        raise ValueError(f"ambiguous species {identifier}: {', '.join(b['key'] for b in matches)}")
    return matches[0]


def effective_species(db: sqlite3.Connection, bird: dict) -> dict:
    from . import review

    return review.effective(db, bird)


def status(db: sqlite3.Connection) -> dict:
    settings = config(db)
    birds = [
        effective_species(db, decode_species(row))
        for row in db.execute("SELECT * FROM species WHERE active=1")
    ]
    covered = sum(bool(bird["artwork"]) for bird in birds)
    phases = {}
    for bird in birds:
        phase = f"blocked:{bird['blocked_reason']}" if bird["blocked_reason"] else bird["state"]
        if phase:
            phases[phase] = phases.get(phase, 0) + 1
    last = db.execute(
        "SELECT * FROM inventory_runs WHERE id=?", (settings.get("last_inventory_run"),)
    ).fetchone()
    return {
        "upstream": settings["upstream_path"],
        "style": settings["style"],
        "total": len(birds),
        "covered": covered,
        "missing": len(birds) - covered,
        "states": phases,
        "batches": db.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
        "inventory": inventory_result(last, changed=False) if last else None,
    }


def list_species(db: sqlite3.Connection, *, state=None, all_species=False, limit=20) -> list[dict]:
    birds = [
        effective_species(db, decode_species(row))
        for row in db.execute("SELECT * FROM species ORDER BY records DESC, key")
    ]
    birds = [b for b in birds if all_species or (b["active"] and b["state"])]
    if state:
        birds = [b for b in birds if b["state"] == state]
    return birds[:limit]


def next_species(db: sqlite3.Connection, batch: str | None) -> dict | None:
    if batch:
        if not db.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
            raise ValueError(f"unknown batch: {batch}")
        query = """SELECT s.* FROM species s JOIN batch_members m ON s.key=m.species_key
        WHERE m.batch_id=? AND s.active=1 AND s.state='missing' AND s.artwork='[]'
        AND s.blocked_reason IS NULL ORDER BY s.records DESC, s.key LIMIT 1"""
        row = db.execute(query, (batch,)).fetchone()
    else:
        row = db.execute("""SELECT s.* FROM species s LEFT JOIN batch_members m
        ON s.key=m.species_key WHERE m.species_key IS NULL AND s.active=1
        AND s.state='missing' AND s.artwork='[]' AND s.blocked_reason IS NULL
        ORDER BY s.records DESC, s.key LIMIT 1""").fetchone()
    return decode_species(row) if row else None


def show_species(db: sqlite3.Connection, identifier: str) -> dict:
    bird = effective_species(db, find_species(db, identifier))
    row = db.execute(
        "SELECT batch_id FROM batch_members WHERE species_key=?", (bird["key"],)
    ).fetchone()
    bird["batch"] = row[0] if row else None
    bird["events"] = [
        dict(e)
        for e in db.execute("SELECT * FROM events WHERE species_key=? ORDER BY id", (bird["key"],))
    ]
    run = db.execute("SELECT * FROM inventory_runs WHERE id=?", (bird["inventory_run"],)).fetchone()
    bird["inventory"] = inventory_result(run, changed=False)
    bird["checklist_provenance"] = {
        k: v for k, v in json.loads(run["checklist_json"]).items() if k != "species"
    }
    return bird


def show_batch(db: sqlite3.Connection, identifier: str) -> dict:
    row = db.execute("SELECT * FROM batches WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise ValueError(f"unknown batch: {identifier}")
    members = db.execute(
        """SELECT s.* FROM species s JOIN batch_members m
        ON s.key=m.species_key WHERE m.batch_id=? ORDER BY s.key""",
        (identifier,),
    )
    return dict(row) | {
        "members": [effective_species(db, decode_species(bird)) for bird in members]
    }


def create_batch(workspace: Path, identifier: str, names: list[str]) -> dict:
    if not names or len(names) > 5:
        raise ValueError("a batch needs one to five species")
    if (
        not identifier
        or len(identifier) > 80
        or not all(c.isascii() and (c.isalnum() or c in "-_") for c in identifier)
    ):
        raise ValueError("batch id must be 1-80 ASCII letters, numbers, hyphens or underscores")
    with mutation_lock(workspace), connect(workspace) as db:
        db.execute("BEGIN IMMEDIATE")
        birds = [find_species(db, name) for name in names]
        keys = {bird["key"] for bird in birds}
        if len(keys) != len(birds):
            raise ValueError("batch species must be unique (including aliases)")
        if db.execute("SELECT 1 FROM batches WHERE id=?", (identifier,)).fetchone():
            existing = show_batch(db, identifier)
            if keys != {bird["key"] for bird in existing["members"]}:
                raise ValueError(f"batch {identifier} already has different members")
            return existing | {"created": False}
        for bird in birds:
            if bird["artwork"]:
                raise ValueError(f"{bird['key']} is already covered")
            if not bird["active"] or bird["state"] != "missing" or bird["blocked_reason"]:
                raise ValueError(f"{bird['key']} is not an active, unblocked missing bird")
            member = db.execute(
                "SELECT batch_id FROM batch_members WHERE species_key=?", (bird["key"],)
            ).fetchone()
            if member:
                raise ValueError(f"{bird['key']} is already in batch {member[0]}")
        runs = {bird["inventory_run"] for bird in birds}
        if len(runs) != 1:
            raise ValueError("batch birds need the same inventory snapshot; refresh inventory")
        revision = db.execute(
            "SELECT upstream_revision FROM inventory_runs WHERE id=?", (runs.pop(),)
        ).fetchone()[0]
        now = datetime.now(UTC).isoformat()
        db.execute("INSERT INTO batches VALUES (?, ?, ?)", (identifier, revision, now))
        for key in sorted(keys):
            db.execute("INSERT INTO batch_members VALUES (?, ?)", (identifier, key))
            db.execute(
                "INSERT INTO events (species_key, batch_id, action, created_at, notes) VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    identifier,
                    "batch-created",
                    now,
                    "Selected for artwork research; no approval implied.",
                ),
            )
        return show_batch(db, identifier) | {"created": True}
