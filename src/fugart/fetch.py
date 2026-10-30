"""Acquire approved original raster scans with a small durable recovery journal."""

import json
import os
import tempfile
import time
import urllib.request
import warnings
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from . import sources, store

MAX_BYTES = 128 * 1024 * 1024
MAX_SECONDS = 120
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "TIFF": ".tif"}


class ProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        sources.validate_url(newurl, scan=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_remote(url: str):
    sources.validate_url(url, scan=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "FuglerammeAUArt/0.1 source-approved scan acquisition"}
    )
    return urllib.request.build_opener(ProviderRedirect()).open(request, timeout=30)


def scan_path(workspace: Path, relative: str) -> Path:
    path = (workspace / relative).resolve()
    if not path.is_relative_to((workspace / "work/scans").resolve()):
        raise ValueError("fetch journal path is outside scan storage")
    return path


def verify_image(path: Path) -> dict:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in EXTENSIONS or max(image.size) < 600:
                    raise ValueError(
                        "scan must be JPEG/PNG/WebP/TIFF with at least a 600 px longest side"
                    )
                info = {"format": image.format, "width": image.width, "height": image.height}
                image.verify()
            with Image.open(path) as image:
                image.load()  # verify alone does not fully decode every supported format.
        return info
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise ValueError("scan exceeds safe image dimensions") from error


def save_job(db, source_id: int, job: dict) -> None:
    db.execute(
        "INSERT INTO fetch_jobs VALUES (?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET record_json=excluded.record_json, updated_at=excluded.updated_at",
        (source_id, json.dumps(job), datetime.now(UTC).isoformat()),
    )
    db.commit()


def download(workspace: Path, db, source: dict) -> dict:
    folder = workspace / "work/scans/.partial"
    folder.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=folder, suffix=".part")
    temporary = Path(temporary_name)
    job = {
        "status": "started",
        "temporary": str(temporary.relative_to(workspace)),
        "requested_url": source["record"]["download_url"],
        "source_fingerprint": source["fingerprint"],
    }
    started = time.monotonic()
    try:
        with os.fdopen(fd, "wb") as file:
            save_job(db, source["id"], job)
            record = stream(file, temporary, job, source, started)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        sources.event(
            db,
            source["species_key"],
            "fetch-failed",
            f"Source revision {source['revision']}; {error}",
        )
        save_job(db, source["id"], job | {"status": "failed", "error": str(error)})
        raise
    verified = job | {"status": "verified", "record": record}
    # Persist verification before promotion: interruption here can resume without HTTP.
    save_job(db, source["id"], verified)
    return verified


def stream(file, temporary: Path, job: dict, source: dict, started: float) -> dict:
    with open_remote(job["requested_url"]) as response:
        resolved_url = sources.validate_url(response.geturl(), scan=True)
        length = response.headers.get("Content-Length")
        expected = int(length) if length is not None else None
        if expected is not None and (expected <= 0 or expected > MAX_BYTES):
            raise ValueError("scan Content-Length is empty or exceeds 128 MiB")
        count = 0
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > MAX_BYTES or time.monotonic() - started > MAX_SECONDS:
                raise ValueError("scan exceeds acquisition byte/time limits")
            file.write(chunk)
        if time.monotonic() - started > MAX_SECONDS:
            raise ValueError("scan exceeds acquisition time limit")
        if count == 0 or (expected is not None and count != expected):
            raise ValueError("scan download is empty or truncated")
        file.flush()
        os.fsync(file.fileno())
        metadata = {
            "requested_url": job["requested_url"],
            "resolved_url": resolved_url,
            "content_type": response.headers.get("Content-Type", ""),
            "etag": response.headers.get("ETag", ""),
            "last_modified": response.headers.get("Last-Modified", ""),
            "bytes": count,
        }
    info = verify_image(temporary)
    digest = sources.sha256(temporary)
    return (
        metadata
        | info
        | {
            "sha256": digest,
            "path": f"work/scans/{digest}{EXTENSIONS[info['format']]}",
            "source_id": source["id"],
            "source_revision": source["revision"],
            "source_fingerprint": source["fingerprint"],
            "acquired_at": datetime.now(UTC).isoformat(),
        }
    )


def promote(workspace: Path, job: dict) -> None:
    record = job["record"]
    destination = scan_path(workspace, record["path"])
    temporary = scan_path(workspace, job["temporary"])
    if destination.exists():
        if sources.sha256(destination) != record["sha256"]:
            raise ValueError("completed scan checksum mismatch; original file was not overwritten")
    else:
        if not temporary.is_file() or sources.sha256(temporary) != record["sha256"]:
            raise ValueError("verified temporary scan checksum mismatch; inspect fetch journal")
        os.link(temporary, destination)
    temporary.unlink(missing_ok=True)


def complete(db, source: dict, key: str, record: dict) -> None:
    db.execute("BEGIN IMMEDIATE")
    bird = store.find_species(db, key)
    if not bird["active"] or bird["blocked_reason"] or bird["artwork"]:
        raise ValueError("bird became inactive or blocked; acquisition retained for recovery")
    db.execute(
        "INSERT INTO downloads VALUES (?, ?, ?)",
        (source["id"], json.dumps(record), datetime.now(UTC).isoformat()),
    )
    db.execute("UPDATE species SET state='downloaded' WHERE key=?", (key,))
    db.execute(
        "UPDATE fetch_jobs SET record_json=?, updated_at=? WHERE source_id=?",
        (
            json.dumps({"status": "complete", "record": record}),
            datetime.now(UTC).isoformat(),
            source["id"],
        ),
    )
    sources.event(
        db, key, "scan-downloaded", f"Source revision {source['revision']}; {record['sha256']}"
    )


def acquire(workspace: Path, identifier: str) -> dict:
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_sources(db)
        bird = store.find_species(db, identifier)
        if not bird["active"] or bird["blocked_reason"] or bird["artwork"]:
            raise ValueError("fetch requires an active, unblocked bird missing artwork")
        source = sources.current(db, bird["key"])
        if (
            not source
            or not source["approval"]
            or source["approval"]["fingerprint"] != source["fingerprint"]
        ):
            raise ValueError(
                "fetch requires human source/species/licence approval of the current revision"
            )
        sources.verify_identity(bird, source)
        sources.verify_evidence(workspace, source)
        existing = db.execute(
            "SELECT record_json FROM downloads WHERE source_id=?", (source["id"],)
        ).fetchone()
        if existing:
            record = json.loads(existing[0])
            path = scan_path(workspace, record["path"])
            if not path.is_file() or sources.sha256(path) != record["sha256"]:
                raise ValueError("completed scan checksum mismatch; refusing silent replacement")
            return record | {"changed": False, "recovered": False}
        if bird["state"] != "sourced":
            raise ValueError("new scan acquisition requires the sourced phase")
        prior = db.execute(
            "SELECT record_json FROM fetch_jobs WHERE source_id=?", (source["id"],)
        ).fetchone()
        job = json.loads(prior[0]) if prior else None
        recovered = bool(job and job["status"] == "verified")
        if not recovered:
            if job and job.get("temporary"):
                scan_path(workspace, job["temporary"]).unlink(missing_ok=True)
                sources.event(
                    db,
                    bird["key"],
                    "fetch-restarted",
                    "Previous incomplete attempt retained in workflow history; partial scan discarded.",
                )
                db.commit()
            job = download(workspace, db, source)
        promote(workspace, job)
        complete(db, source, bird["key"], job["record"])
        return job["record"] | {"changed": True, "recovered": recovered}
