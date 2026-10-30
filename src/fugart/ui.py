"""fugart ui: a local page that walks each bird through its gates and records your decisions.

Every action calls the same function as the CLI command it stands for, so the UI adds no
second path round the checks. Approvals made here are human decisions, recorded under the
reviewer name typed in the page. "Needs work" notes go to `work/feedback.jsonl`, which
`fugart feedback` lists for whoever (or whichever agent) picks the work up.

The server binds to 127.0.0.1 only, refuses other Host headers (DNS rebinding), and every
POST must carry the per-launch token embedded in the page.
"""

import hashlib
import json
import mimetypes
import secrets
import threading
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from . import boxing, cutout, fetch, imports, review, sources, store, upstream

PAGE = Path(__file__).with_name("ui.html")
# Gates in order; each phase says which gate the bird is waiting at.
GATES = ("source", "scan", "cut", "upstream", "box", "final", "pr")
WAITING = {
    "missing": "source",
    "researching": "source",
    "sourced": "scan",
    "downloaded": "cut",
    "cut": "cut",
    "needs-review": "upstream",
    "imported": "box",
    "boxed": "final",
    "approved": "pr",
    "ready-pr": "pr",
    "submitted": "pr",
    "merged": "done",
}
SHOWN = (
    "crop.png",
    "cut.png",
    "final.png",
    "preview.png",
    "debug-mask.png",
    "lost.jpg",
    "qa.json",
    "tone.json",
    "disclosure.md",
)
# The native review's layers, written by upstream's platereview/build.py. All four are the
# same size and line up pixel for pixel, so the page can wipe and sample between them.
REVIEW_LAYERS = ("cut.png", "plate.png", "scan.png", "flags.jpg")
THUMB = (400, 900, 1600, 2400)


# --- feedback queue -------------------------------------------------------------------------


def feedback_path(workspace: Path) -> Path:
    return workspace / "work/feedback.jsonl"


def feedback(workspace: Path, key: str | None = None, *, open_only: bool = False) -> list[dict]:
    """Notes in the order they were left, each with its resolution if it has one."""
    path = feedback_path(workspace)
    notes, resolved = {}, {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("resolves"):
                resolved[entry["resolves"]] = entry
            else:
                notes[entry["id"]] = entry
    out = []
    for entry in notes.values():
        entry = entry | {"resolution": resolved.get(entry["id"])}
        if key and entry["species"] != key:
            continue
        if open_only and entry["resolution"]:
            continue
        out.append(entry)
    return out


def append_feedback(workspace: Path, entry: dict) -> dict:
    path = feedback_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": secrets.token_hex(4), "at": datetime.now(UTC).isoformat()} | entry
    with store.mutation_lock(workspace), path.open("a") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def leave_feedback(workspace: Path, key: str, gate: str, notes: str, by: str) -> dict:
    if gate not in GATES or not notes.strip() or not by.strip():
        raise ValueError("feedback needs a gate, notes and a name")
    with store.connect(workspace, readonly=True) as db:
        key = store.find_species(db, key)["key"]
    return append_feedback(
        workspace, {"species": key, "gate": gate, "notes": notes.strip(), "by": by.strip()}
    )


def resolve_feedback(workspace: Path, note: str, notes: str, by: str) -> dict:
    if not notes.strip() or not by.strip():
        raise ValueError("a resolution needs notes and a name")
    if note not in {entry["id"] for entry in feedback(workspace, open_only=True)}:
        raise ValueError(f"no open feedback {note}")
    return append_feedback(workspace, {"resolves": note, "notes": notes.strip(), "by": by})


# --- state for the page ---------------------------------------------------------------------


def relative(workspace: Path, path: Path) -> str | None:
    path = path.resolve()
    return str(path.relative_to(workspace)) if path.is_relative_to(workspace) else None


def gate_states(phase: str | None) -> dict:
    waiting = WAITING.get(phase or "", "source")
    if waiting == "done":
        return dict.fromkeys(GATES, "done")
    at = GATES.index(waiting)
    return {g: "done" if i < at else "current" if i == at else "todo" for i, g in enumerate(GATES)}


def cut_files(workspace: Path, current: dict | None) -> dict:
    """The current revision's files, falling back to the candidate folder it was filed from."""
    if not current:
        return {}
    folder = workspace / current["path"]
    log = folder / "log.json"
    candidate = None
    if log.is_file():
        made = json.loads(log.read_text()).get("candidate_path")
        candidate = Path(made) if made else None
    files = {}
    for name in SHOWN:
        for base in (folder, candidate):
            if base and (base / name).is_file() and relative(workspace, base / name):
                files[name] = relative(workspace, base / name)
                break
    return files


def bird(workspace: Path, key: str) -> dict:
    with store.connect(workspace, readonly=True) as db:
        about = store.show_species(db, key)
        key = about["key"]
        root = Path(store.config(db)["upstream_path"])
        source = sources.show(db, key)
        cuts = cutout.show(db, workspace, key) or {"current": None, "revisions": []}
        made = imports.show(db, key)
        box = boxing.show(db, key)
        built = review.show(db, workspace, key)
    current = cuts["current"]
    files = cut_files(workspace, current)
    qa = json.loads((workspace / files["qa.json"]).read_text()) if "qa.json" in files else None
    tone = (
        json.loads((workspace / files["tone.json"]).read_text()) if "tone.json" in files else None
    )
    disclosure = (
        (workspace / files["disclosure.md"]).read_text() if "disclosure.md" in files else ""
    )
    scan = None
    if source["download"]:
        scan = relative(workspace, fetch.scan_path(workspace, source["download"]["path"]))
    plate = made and made["state"] == "complete" and made["record"].get("plate")
    geometry = box and box["state"] == "complete" and box["record"].get("geometry_entry")
    review_files = {}
    if built and built.get("record"):
        folder = workspace / built["record"].get("folder", "")
        names = {n: n for n in ("index.html", "preview/panel.png", "preview/web.png")}
        names |= {n: f"img/{key}/{n}" for n in REVIEW_LAYERS}
        for name, path in names.items():
            if (folder / path).is_file():
                review_files[name] = relative(workspace, folder / path)
    phase = about["state"]
    return {
        "key": key,
        "common_name": about["common_name"],
        "scientific_name": about["scientific_name"],
        "batch": about["batch"],
        "phase": phase,
        "blocked": about["blocked_reason"],
        "gates": gate_states(phase),
        "events": about["events"][-30:],
        "source": source["current"],
        "scan": scan,
        "cut": current
        and {
            "revision": current["revision"],
            "intact": current["intact"],
            "files": files,
            "qa": qa,
            "tone": tone,
            "disclosure": disclosure,
        },
        "plate": plate and bool((root / plate).is_file()) and plate,
        "box": geometry
        and {
            "box": geometry["box"],
            "cut": geometry["cut"],
            "method": box["record"].get("box_method", "human editor"),
            "notes": box["record"].get("notes", ""),
            "can_edit": phase == "imported"
            or (phase in ("boxed", "approved") and made["record"].get("mode") == "replace"),
        },
        "review": built
        and {
            "id": built["id"],
            "valid": built["valid"],
            "approved": built["approved"],
            "approval": built["approval"],
            "files": review_files,
        },
        "feedback": feedback(workspace, key),
    }


def overview(workspace: Path) -> dict:
    with store.connect(workspace, readonly=True) as db:
        status = store.status(db)
        batches = [
            store.show_batch(db, row[0])
            for row in db.execute("SELECT id FROM batches ORDER BY created_at DESC")
        ]
        root = Path(store.config(db)["upstream_path"])
    for batch in batches:
        for member in batch["members"]:
            member["gates"] = gate_states(member["state"])
            member["open_feedback"] = len(feedback(workspace, member["key"], open_only=True))
        batch["ready"] = bool(batch["members"]) and all(
            m["state"] in ("approved", "ready-pr", "submitted", "merged") for m in batch["members"]
        )
    return {
        "status": {k: status[k] for k in ("total", "covered", "missing", "states")}
        | {"region": (status["inventory"] or {}).get("region")},
        "upstream": str(root),
        "batches": batches,
        "open_feedback": len(feedback(workspace, open_only=True)),
    }


def pull_request(workspace: Path, batch: str) -> dict:
    """What the PR would carry: the upstream diff and the drafted body. Nothing is posted."""
    with store.connect(workspace, readonly=True) as db:
        members = store.show_batch(db, batch)["members"]
        root = Path(store.config(db)["upstream_path"])
    body = workspace / f"work/research/{batch}/pr-body.md"
    return {
        "batch": batch,
        "ready": all(m["state"] == "approved" for m in members),
        "birds": [{"key": m["key"], "phase": m["state"]} for m in members],
        "diff": upstream.run(["git", "diff", "--stat"], root),
        "untracked": upstream.run(["git", "ls-files", "--others", "--exclude-standard"], root),
        "body": body.read_text() if body.is_file() else "",
        "body_path": relative(workspace, body),
    }


# --- files ----------------------------------------------------------------------------------


def servable(workspace: Path, root: Path, path: str) -> Path:
    """Only work/ in the workspace and the style's artwork in the checkout are served."""
    if path.startswith("upstream/"):
        base, rest = (root / "assets/artwork").resolve(), path.removeprefix("upstream/")
        target = (root / rest).resolve()
    else:
        base, target = (workspace / "work").resolve(), (workspace / path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise FileNotFoundError(path)
    return target


def thumbnail(workspace: Path, target: Path, width: int) -> Path:
    """A cached WebP no wider than `width`; scans are 75 MB TIFFs, too big for a browser."""
    width = min((w for w in THUMB if w >= width), default=THUMB[-1])
    stat = target.stat()
    name = hashlib.sha256(f"{target}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode())
    cache = workspace / ".fugart/ui-cache" / f"{name.hexdigest()[:24]}.webp"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(target) as im:
            im.thumbnail((width, width * 4))
            keep = im.mode in ("RGBA", "LA") or "transparency" in im.info
            im = im.convert("RGBA" if keep else "RGB")
            part = cache.with_suffix(".part")
            im.save(part, "WEBP", quality=88, method=4)
            part.replace(cache)
    return cache


# --- server ---------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "fugart-ui"
    workspace: Path
    root: Path
    token: str
    hosts: set[str]

    def log_message(self, format, *args):
        pass

    def send(self, status: int, body: bytes, kind: str, cache: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def reply(self, value, status: int = 200):
        self.send(status, json.dumps(value, default=str).encode(), "application/json")

    def allowed(self) -> bool:
        if self.headers.get("Host") not in self.hosts:
            self.send(403, b"wrong host", "text/plain")
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/":
                page = PAGE.read_text().replace("__TOKEN__", self.token)
                return self.send(200, page.encode(), "text/html; charset=utf-8")
            if url.path == "/api/overview":
                return self.reply(overview(self.workspace))
            if url.path.startswith("/api/bird/"):
                return self.reply(bird(self.workspace, url.path.removeprefix("/api/bird/")))
            if url.path.startswith("/api/pr/"):
                return self.reply(pull_request(self.workspace, url.path.removeprefix("/api/pr/")))
            if url.path == "/api/feedback":
                return self.reply(feedback(self.workspace, open_only=True))
            if url.path.startswith("/files/"):
                target = servable(self.workspace, self.root, url.path.removeprefix("/files/"))
                if "w" in query:
                    target = thumbnail(self.workspace, target, int(query["w"][0]))
                kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return self.send(200, target.read_bytes(), kind, cache="w" in query)
            self.send(404, b"not found", "text/plain")
        except (FileNotFoundError, KeyError):
            self.send(404, b"not found", "text/plain")
        except ValueError as error:
            self.reply({"error": str(error)}, 400)

    def do_POST(self):
        if not self.allowed():
            return
        if not secrets.compare_digest(self.headers.get("X-Fugau-Token", ""), self.token):
            return self.reply({"error": "missing or wrong page token; reload the page"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            self.reply(act(self.workspace, urlparse(self.path).path, data))
        except (ValueError, TypeError, KeyError, OSError) as error:
            self.reply({"error": str(error)}, 400)


def act(workspace: Path, path: str, data: dict):
    """One POST, one fugart function. Approvals need the reviewer's name and notes, as on the
    CLI, and the page asks for an explicit tick where the CLI asks for a --confirm flag."""
    who = str(data.get("reviewer", "")).strip()
    notes = str(data.get("notes", ""))
    key = str(data.get("species", ""))
    if path == "/api/source/approve":
        if data.get("confirm") is not True:
            raise ValueError("tick that you checked the source, species and licence")
        return sources.approve(workspace, key, int(data["revision"]), who, notes)
    if path == "/api/box":
        box = [float(v) for v in data["box"]]
        method = f"human, drawn in fugart ui by {who}" if who else "fugart ui"
        if not who:
            raise ValueError("enter your name before saving a box")
        return boxing.propose(workspace, key, box, notes or "Drawn in fugart ui", method)
    if path == "/api/review/build":
        return review.build(workspace, key)
    if path == "/api/review/approve":
        if data.get("confirm") is not True:
            raise ValueError("tick that you looked at the plate at full size")
        return review.approve(workspace, key, int(data["review"]), who, notes)
    if path == "/api/feedback":
        return leave_feedback(workspace, key, str(data.get("gate", "")), notes, who)
    if path == "/api/feedback/resolve":
        return resolve_feedback(workspace, str(data.get("id", "")), notes, who)
    raise ValueError(f"unknown action {path}")


def make_server(workspace: Path, port: int) -> ThreadingHTTPServer:
    with store.connect(workspace, readonly=True) as db:
        root = Path(store.config(db)["upstream_path"]).resolve()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_address[1]
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "workspace": workspace.resolve(),
            "root": root,
            "token": secrets.token_urlsafe(24),
            "hosts": {f"127.0.0.1:{port}", f"localhost:{port}"},
        },
    )
    server.RequestHandlerClass = handler
    return server


def serve(workspace: Path, port: int = 8090, open_browser: bool = True) -> dict:
    server = make_server(workspace, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"fugart ui on {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"url": url, "stopped": True}
