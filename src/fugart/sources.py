"""Immutable source evidence and explicitly recorded human source approvals."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from . import store

TEXT_FIELDS = (
    "scientific_name",
    "historical_name",
    "work",
    "work_date",
    "provider",
    "source_key",
    "plate_url",
    "download_url",
    "licence",
    "licence_url",
    "licence_evidence",
    "taxonomy_notes",
)
SCAN_HOSTS = {
    "archive.org",
    "upload.wikimedia.org",
    "www.gutenberg.org",
    "gutenberg.org",
    "www.biodiversitylibrary.org",
    "biodiversitylibrary.org",
    "digital.lib.ku.edu",
}


def validate_url(url: str, *, scan: bool = False) -> str:
    if not isinstance(url, str) or not url.strip():
        raise TypeError("source URL must be nonempty text")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("source URLs must be credential-free HTTPS URLs on port 443")
    if (
        scan
        and parsed.hostname not in SCAN_HOSTS
        and not re.fullmatch(r"ia\d+\.(?:us|eu)\.archive\.org", parsed.hostname)
    ):
        raise ValueError(f"scan host is not an approved provider: {parsed.hostname}")
    return url


def sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def publish_bytes(path: Path, data: bytes) -> None:
    expected = hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if sha256(path) != expected:
            raise ValueError(f"evidence checksum mismatch: {path}")
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="evidence-")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_record(workspace: Path, path: Path) -> tuple[dict, list[bytes]]:
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("source record must use schema_version 1")
    unknown = (
        set(data)
        - set(TEXT_FIELDS)
        - {"schema_version", "creators", "taxonomy_urls", "evidence", "notes"}
    )
    if unknown:
        raise ValueError(f"unknown source fields: {', '.join(sorted(unknown))}")
    for field in TEXT_FIELDS:
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"source needs nonempty {field}")
        data[field] = data[field].strip()
    for field in ("creators", "taxonomy_urls"):
        if (
            not isinstance(data.get(field), list)
            or not data[field]
            or not all(isinstance(x, str) and x.strip() for x in data[field])
        ):
            raise ValueError(f"source needs a nonempty {field} list")
    for field in ("plate_url", "licence_url", "download_url"):
        validate_url(data[field], scan=field == "download_url")
    for url in data["taxonomy_urls"]:
        validate_url(url)
    if not isinstance(data.get("notes", ""), str):
        raise TypeError("source notes must be text")
    data.setdefault("notes", "")
    if not isinstance(data.get("evidence"), list) or not data["evidence"]:
        raise ValueError("source needs retained evidence files")
    evidence, blobs = [], []
    for item in data["evidence"]:
        if not isinstance(item, dict) or set(item) != {"url", "file"}:
            raise ValueError("each evidence item needs url and file")
        validate_url(item["url"])
        if not isinstance(item["file"], str) or not item["file"].strip():
            raise ValueError("evidence file must be a nonempty path")
        file = (path.parent / item["file"]).resolve()
        if not file.is_relative_to(workspace) or not file.is_file():
            raise ValueError("evidence files must exist inside this companion workspace")
        if file.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("evidence file exceeds 8 MiB")
        blob = file.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        evidence.append(
            {"url": item["url"], "sha256": digest, "path": f"work/evidence/{digest}.bin"}
        )
        blobs.append(blob)
    data["evidence"] = evidence
    return data, blobs


def current(db: sqlite3.Connection, key: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM sources WHERE species_key=? ORDER BY revision DESC LIMIT 1", (key,)
    ).fetchone()
    if row is None:
        return None
    source = dict(row)
    source["record"] = json.loads(source.pop("record_json"))
    approval = db.execute(
        "SELECT * FROM source_reviews WHERE source_id=?", (source["id"],)
    ).fetchone()
    source["approval"] = dict(approval) if approval else None
    return source


def verify_evidence(workspace: Path, source: dict) -> None:
    for item in source["record"]["evidence"]:
        path = workspace / item["path"]
        if not path.is_file() or sha256(path) != item["sha256"]:
            raise ValueError(f"source evidence checksum mismatch: {path}")


def verify_identity(bird: dict, source: dict) -> None:
    if source["record"]["scientific_name"] != bird["scientific_name"]:
        raise ValueError("scientific name changed; record and review an updated source revision")


def event(db, key: str, action: str, notes: str) -> None:
    db.execute(
        "INSERT INTO events (species_key, action, created_at, notes) VALUES (?, ?, ?, ?)",
        (key, action, datetime.now(UTC).isoformat(), notes),
    )


def record(workspace: Path, identifier: str, path: Path) -> dict:
    data, blobs = read_record(workspace, path)
    fingerprint = hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_sources(db)
        db.execute("BEGIN IMMEDIATE")
        bird = store.find_species(db, identifier)
        if data["scientific_name"] != bird["scientific_name"]:
            raise ValueError("source scientific_name must match the selected canonical species")
        if (
            not bird["active"]
            or bird["artwork"]
            or bird["state"] not in ("missing", "researching", "sourced", "downloaded")
        ):
            raise ValueError("source editing is limited to active, missing artwork before cutting")
        old = current(db, bird["key"])
        if old and old["fingerprint"] == fingerprint:
            verify_evidence(workspace, old)
            return old | {"changed": False}
        for item, blob in zip(data["evidence"], blobs, strict=True):
            publish_bytes(workspace / item["path"], blob)
        revision = old["revision"] + 1 if old else 1
        db.execute(
            "INSERT INTO sources (species_key, revision, fingerprint, record_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (bird["key"], revision, fingerprint, json.dumps(data), datetime.now(UTC).isoformat()),
        )
        db.execute("UPDATE species SET state='researching' WHERE key=?", (bird["key"],))
        event(
            db,
            bird["key"],
            "source-recorded",
            f"Revision {revision}, fingerprint {fingerprint}; previous evidence retained.",
        )
        return current(db, bird["key"]) | {"changed": True}


def approve(workspace: Path, identifier: str, revision: int, reviewer: str, notes: str) -> dict:
    if not reviewer.strip() or not notes.strip():
        raise ValueError("human reviewer and review notes are required")
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_sources(db)
        db.execute("BEGIN IMMEDIATE")
        bird = store.find_species(db, identifier)
        if bird["blocked_reason"] or not bird["active"]:
            raise ValueError("source approval requires an active, unblocked bird")
        if bird["artwork"]:
            raise ValueError("bird is now covered; refresh and inspect the existing artwork")
        source = current(db, bird["key"])
        if not source or revision != source["revision"]:
            raise ValueError("approve the current revision after reviewing its evidence")
        verify_identity(bird, source)
        verify_evidence(workspace, source)
        if source["approval"]:
            return source | {"changed": False}
        db.execute(
            "INSERT INTO source_reviews (source_id, reviewer, notes, fingerprint, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                source["id"],
                reviewer.strip(),
                notes.strip(),
                source["fingerprint"],
                datetime.now(UTC).isoformat(),
            ),
        )
        db.execute("UPDATE species SET state='sourced' WHERE key=?", (bird["key"],))
        event(db, bird["key"], "source-approved", f"Revision {revision}; {reviewer}: {notes}")
        return current(db, bird["key"]) | {"changed": True}


def show(db: sqlite3.Connection, identifier: str) -> dict:
    bird = store.find_species(db, identifier)
    if store.config(db)["schema_version"] == "1":
        return {"current": None, "history": [], "download": None}
    source = current(db, bird["key"])
    history = [
        dict(row)
        for row in db.execute(
            "SELECT id, revision, fingerprint, created_at FROM sources WHERE species_key=? ORDER BY revision",
            (bird["key"],),
        )
    ]
    download = (
        db.execute(
            "SELECT record_json FROM downloads WHERE source_id=?", (source["id"],)
        ).fetchone()
        if source
        else None
    )
    return {
        "current": source,
        "history": history,
        "download": json.loads(download[0]) if download else None,
    }


def block(workspace: Path, identifier: str, reason: str | None, notes: str) -> dict:
    if not notes.strip() or (reason is not None and reason not in store.BLOCK_REASONS):
        raise ValueError("blocking/unblocking needs a valid reason and nonempty notes")
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        db.execute("BEGIN IMMEDIATE")
        bird = store.find_species(db, identifier)
        if bird["state"] is None or bird["state"] == "merged":
            raise ValueError("only birds in the artwork workflow can be blocked")
        db.execute(
            "UPDATE species SET blocked_reason=?, notes=? WHERE key=?",
            (reason, notes.strip(), bird["key"]),
        )
        event(
            db,
            bird["key"],
            "blocked" if reason else "unblocked",
            f"{reason or 'resolved'}: {notes}",
        )
        return store.find_species(db, bird["key"])
