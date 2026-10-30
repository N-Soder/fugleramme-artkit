"""Replace a boxed plate explicitly, preserving cuts and recovering partial writes."""

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from . import cutout, fetch, imports, review, sources, store, upstream

# How a candidate was made, kept beside it: scripts, their parameters and their reports.
RECIPE = (
    "repair.py",
    "repair-notes.json",
    "qa.json",
    "params.json",
    "vignette.py",
    "tone.py",
    "tone.json",
    "cut-stats.json",
    "final-finish.png",
    "qa.py",
    "lost.jpg",
    "disclosure.md",
)


def atomic_write(path: Path, data: bytes):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(data)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def prepare(root: Path, workspace: Path, folder: Path) -> dict:
    result = upstream.native(
        root,
        workspace,
        "-c",
        "import sys; from pathlib import Path; "
        "from tools.add_bird import prepare, write_plate, preview; "
        'p=Path(sys.argv[1]); im=prepare(p/"final.png"); '
        'write_plate(im,p/"shipped.webp"); preview(im,p/"preview.png")',
        str(folder),
    )
    if result["returncode"]:
        raise ValueError(f"native preparation failed: {result['stderr']}")
    return result


def native_crop(root: Path, workspace: Path, spec: Path) -> dict:
    return upstream.native(root, workspace, cutout.PLATE, "crop", str(spec))


def same_pixels(first: Path, second: Path) -> bool:
    with Image.open(first) as a, Image.open(second) as b:
        return a.mode == b.mode and a.size == b.size and a.tobytes() == b.tobytes()


def recrop_plan(candidate: Path, old: Path, scan_file: Path) -> dict:
    """A wider or narrower crop is still the approved scan when plate.py can reproduce it.

    The candidate says how with a spec.json naming the approved scan, a box and the turn
    the previous cut used, so the old bird box can follow through the scan's own pixels.
    """
    if not (candidate / "spec.json").is_file() or not (old / "spec.json").is_file():
        raise ValueError("replacement must retain the original approved scan crop pixels")
    spec = json.loads((candidate / "spec.json").read_text())
    previous = json.loads((old / "spec.json").read_text())
    if not isinstance(spec, dict) or spec.get("scan") != str(scan_file):
        raise ValueError(f"recrop spec scan must be the approved scan: {scan_file}")
    if spec.get("rotate") != previous.get("rotate"):
        raise ValueError("recrop must keep the previous cut's rotate")
    boxes = spec.get("box"), previous.get("box")
    if not all(
        isinstance(box, list)
        and len(box) == 4
        and all(isinstance(v, (int, float)) for v in box)
        and box[0] < box[2]
        and box[1] < box[3]
        for box in boxes
    ):
        raise ValueError("recrop needs a scan box in its spec and in the previous cut's")
    return {"spec": spec, "frames": (boxes[1], boxes[0])}


def project_box(old_cut: Path, new_cut: Path, box: list, frames=None) -> list:
    """Carry a bird box from one cut to the next.

    Both cuts are in their crop's pixels. `frames` is the scan box of each crop when the
    replacement was cropped differently from the same scan; without it the crops are one.
    """
    with Image.open(old_cut) as old, Image.open(new_cut) as new:
        ox, oy, right, bottom = old.convert("RGBA").getchannel("A").getbbox()
        nx, ny, nr, nb = new.convert("RGBA").getchannel("A").getbbox()
        sizes = old.size, new.size
    corners = [
        ox + box[0] * (right - ox),
        oy + box[1] * (bottom - oy),
        ox + box[2] * (right - ox),
        oy + box[3] * (bottom - oy),
    ]
    if frames:
        (was, now), ((ow, oh), (nw, nh)) = frames, sizes
        for i, (length, old_length, new_length) in enumerate(
            [(0, ow, nw), (1, oh, nh), (0, ow, nw), (1, oh, nh)]
        ):
            scan = was[length] + corners[i] * (was[length + 2] - was[length]) / old_length
            corners[i] = (scan - now[length]) * new_length / (now[length + 2] - now[length])
    projected = [
        (corners[0] - nx) / (nr - nx),
        (corners[1] - ny) / (nb - ny),
        (corners[2] - nx) / (nr - nx),
        (corners[3] - ny) / (nb - ny),
    ]
    projected = [max(0, min(1, v)) for v in projected]
    if not (projected[0] < projected[2] and projected[1] < projected[3]):
        raise ValueError("previous bird box lies outside the replacement")
    return projected


def resume(db, workspace: Path, root: Path, row: dict) -> dict:
    record = json.loads(row["record_json"])
    bird = store.find_species(db, row["species_key"])
    source = sources.current(db, bird["key"])
    if not bird["active"] or bird["blocked_reason"]:
        raise ValueError("replacement requires an active unblocked bird")
    sources.verify_evidence(workspace, source)
    sources.verify_identity(bird, source)
    if (
        source["fingerprint"] != record["source_fingerprint"]
        or not source["approval"]
        or source["approval"]["fingerprint"] != source["fingerprint"]
    ):
        raise ValueError("approved source changed during replacement")
    if (
        sources.sha256(fetch.scan_path(workspace, record["scan"]["path"]))
        != record["scan"]["sha256"]
    ):
        raise ValueError("original scan changed during replacement")
    current = cutout.show(db, workspace, bird["key"])["current"]
    if not current or not current["intact"] or current["revision"] != record["cut_revision"]:
        raise ValueError("replacement cut changed")
    if upstream.run(["git", "rev-parse", "HEAD"], root) != record["upstream_revision"]:
        raise ValueError("upstream HEAD moved during replacement")
    allowed = {record["plate"], record["geometry"]}
    if set(upstream.changed_paths(root)) - set(record["changed_before"]) - allowed:
        raise ValueError("unrelated changes during replacement")
    for path, digest in record["changed_before"].items():
        if path not in allowed and sources.sha256(root / path) != digest:
            raise ValueError(f"previously journaled file changed: {path}")
    if sources.sha256(root / record["plate"]) not in (
        record["previous_plate_sha256"],
        record["plate_sha256"],
    ):
        raise ValueError("plate conflicts with replacement journal")
    geometry = json.loads((root / record["geometry"]).read_text())
    if geometry not in (record["geometry_before"], record["geometry_after"]):
        raise ValueError("geometry conflicts with replacement journal")
    atomic_write(root / record["plate"], (workspace / record["staged_plate"]).read_bytes())
    atomic_write(
        root / record["geometry"],
        json.dumps(record["geometry_after"], indent=1, sort_keys=True).encode() + b"\n",
    )
    if sources.sha256(root / record["plate"]) != record["plate_sha256"]:
        raise ValueError("replacement plate verification failed")
    record["geometry_sha256"] = sources.sha256(root / record["geometry"])
    record["outcome"] = "complete"
    db.execute(
        "UPDATE operations SET state='complete', record_json=?, updated_at=? WHERE id=?",
        (json.dumps(record), datetime.now(UTC).isoformat(), row["id"]),
    )
    db.execute("UPDATE species SET state='boxed' WHERE key=?", (bird["key"],))
    sources.event(
        db,
        bird["key"],
        "artwork-replaced",
        f"Operation {row['id']}; box projected from previous human box; final sign-off pending",
    )
    return {
        "species": bird["key"],
        "phase": "boxed",
        "operation": row["id"],
        "cut_revision": record["cut_revision"],
        "geometry_entry": record["geometry_entry"],
    }


def replace(workspace: Path, identifier: str, candidate: Path, notes: str) -> dict:
    if not notes.strip():
        raise ValueError("replacement needs edit notes")
    with store.mutation_lock(workspace), store.connect(workspace) as db:
        store.require_operations(db)
        bird = store.find_species(db, identifier)
        root = Path(store.config(db)["upstream_path"])
        upstream.validate_checkout(root, workspace)
        pending = db.execute(
            "SELECT * FROM operations WHERE species_key=? AND kind='replace' AND state='started'",
            (bird["key"],),
        ).fetchone()
        if pending:
            return resume(db, workspace, root, dict(pending))
        if (
            not bird["active"]
            or bird["blocked_reason"]
            or bird["state"] not in ("boxed", "approved")
        ):
            raise ValueError("replacement requires an active unblocked boxed bird")
        before = review.snapshot(db, workspace, bird)
        baseline = imports.accounted_changes(db, root)
        source = sources.current(db, bird["key"])
        scan = json.loads(
            db.execute(
                "SELECT record_json FROM downloads WHERE source_id=?", (source["id"],)
            ).fetchone()[0]
        )
        old = workspace / f"work/cuts/{bird['key']}/r{before['cut_revision']:03d}"
        digests = {
            name: sources.sha256(candidate / name) for name in ("crop.png", "cut.png", "final.png")
        }
        recrop = None
        if not same_pixels(candidate / "crop.png", old / "crop.png"):
            recrop = recrop_plan(candidate, old, fetch.scan_path(workspace, scan["path"]))
        with Image.open(candidate / "crop.png") as crop, Image.open(candidate / "cut.png") as cut:
            if crop.size != cut.size or cut.mode != "RGBA" or not cut.getchannel("A").getbbox():
                raise ValueError("replacement cut must be RGBA in original crop coordinates")
        last = imports.show(db, bird["key"])
        if last["kind"] == "replace" and last["record"]["candidate_sha256"] == digests:
            return {
                "species": bird["key"],
                "phase": bird["state"],
                "operation": last["id"],
                "cut_revision": before["cut_revision"],
                "geometry_entry": before["geometry_entry"],
            }
        revision, folder = cutout.allocate(db, workspace, bird["key"])
        if recrop:
            note = cutout.stage_spec(
                recrop["spec"],
                (candidate / "spec.json").read_bytes(),
                fetch.scan_path(workspace, scan["path"]),
                folder,
            )
            made = native_crop(root, workspace, folder / "spec.json")
            if (
                made["returncode"]
                or not (folder / "crop.png").is_file()
                or not same_pixels(folder / "crop.png", candidate / "crop.png")
            ):
                raise ValueError(
                    "candidate crop is not the native crop of the approved scan; "
                    f"see {folder.relative_to(workspace)}"
                )
            recrop["commands"] = [made]
            recrop["note"] = (
                f"Approved scan re-cropped by plate.py to box {recrop['spec']['box']}; "
                "cut outside plate.py, recipe files kept beside it."
            )
            recrop["disclosures"] = [note] if note else []
        # Preserve inputs and edit disclosures alongside the new immutable production files.
        for name in ("crop.png", "cut.png", "final.png", *RECIPE):
            if (candidate / name).is_file():
                shutil.copyfile(candidate / name, folder / name)
        if any(sources.sha256(folder / name) != digest for name, digest in digests.items()):
            raise ValueError("candidate changed while copying replacement inputs")
        command = prepare(root, workspace, folder)
        staged = folder / "shipped.webp"
        prepared = candidate / "classic/birds" / f"{bird['key']}.webp"
        if prepared.exists() and sources.sha256(prepared) != sources.sha256(staged):
            raise ValueError("native preparation differs from the displayed candidate")
        if (
            imports.accounted_changes(db, root) != baseline
            or review.snapshot(db, workspace, bird) != before
        ):
            raise ValueError("inputs changed during replacement preparation")
        made = last["record"]
        geometry_path = str(Path(made["manifest"]).with_name("geometry.json"))
        geometry_before = json.loads((root / geometry_path).read_text())
        with Image.open(staged) as im:
            size = list(im.size)
        # Native trim (including halo) defines the normalization dimensions.
        from . import boxing

        trimmed = boxing.measure(root, workspace, str(staged))
        entry = {
            "box": project_box(
                old / "cut.png",
                folder / "cut.png",
                before["geometry_entry"]["box"],
                recrop and recrop["frames"],
            ),
            "cut": trimmed,
        }
        geometry_after = geometry_before | {made["manifest_key"]: entry}
        shutil.copyfile(root / made["plate"], folder / "previous.webp")
        shutil.copyfile(root / geometry_path, folder / "previous-geometry.json")
        log = {
            "species": bird["key"],
            "revision": revision,
            "source_revision": source["revision"],
            "scan": scan,
            "upstream": {"revision": before["upstream_revision"], "dirty": bool(baseline)},
            "notes": notes.strip(),
            "warnings": [],
            "disclosures": [
                "Explicit replacement, no add_bird CLI/importer invocation.",
                *(
                    [recrop["note"], *recrop["disclosures"]]
                    if recrop
                    else ["Deterministic manual scenery cleanup; original scan crop unchanged."]
                ),
                "Box reprojected from previous human box; new quality sign-off pending.",
            ],
            "commands": [*(recrop["commands"] if recrop else []), command],
            "candidate_sha256": digests,
            "candidate_path": str(candidate),
        }
        job = {
            "folder": folder,
            "workspace": workspace,
            "key": bird["key"],
            "source_id": source["id"],
            "made": {},
            "log": log,
        }
        cutout.write_log(job)
        cutout.register(db, workspace, job, final=True)
        record = {
            name: made[name] for name in ("plate", "manifest", "manifest_key", "manifest_entry")
        }
        record.update(
            mode="replace",
            source_revision=source["revision"],
            source_fingerprint=source["fingerprint"],
            scan=scan,
            cut_revision=revision,
            upstream_revision=before["upstream_revision"],
            candidate_sha256=digests,
            image={
                "path": str((folder / "final.png").relative_to(workspace)),
                "sha256": digests["final.png"],
            },
            staged_plate=str(staged.relative_to(workspace)),
            size=size,
            plate_sha256=sources.sha256(staged),
            previous_plate_sha256=before["plate_sha256"],
            previous_import_operation=before["import_operation"],
            previous_box_operation=before["box_operation"],
            changed_before=baseline,
            geometry=geometry_path,
            geometry_key=made["manifest_key"],
            geometry_before=geometry_before,
            geometry_after=geometry_after,
            geometry_entry=entry,
            cut=trimmed,
            box_method="reproject previous human box through unchanged source crop; agent checked; pending final sign-off",
            commands=[command],
            notes=notes.strip(),
        )
        now = datetime.now(UTC).isoformat()
        operation = db.execute(
            "INSERT INTO operations (species_key,kind,state,record_json,created_at,updated_at) VALUES (?,'replace','started',?,?,?)",
            (bird["key"], json.dumps(record), now, now),
        ).lastrowid
        record["import_operation"] = operation
        db.execute(
            "UPDATE operations SET record_json=? WHERE id=?", (json.dumps(record), operation)
        )
        db.commit()
        return resume(
            db,
            workspace,
            root,
            {"id": operation, "species_key": bird["key"], "record_json": json.dumps(record)},
        )
