"""Local artwork backlog, provenance, scan acquisition, native cuts and provisional imports."""

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path

from . import boxing, checklist, cutout, fetch, imports, review, revisions, sources, store, ui


def positive_limit(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000")
    return number


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="fugart", description=__doc__)
    command.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = command.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="bind a local workspace to a separate checkout")
    init.add_argument("--upstream", type=Path, required=True)
    init.add_argument("--json", action="store_true")
    build = commands.add_parser("checklist", help="build a dated checklist for a country from GBIF")
    build_commands = build.add_subparsers(dest="checklist_command", required=True)
    build_one = build_commands.add_parser("build", help="query GBIF and join to BirdNET birds")
    build_one.add_argument("--country", required=True, help="two-letter ISO code, such as NZ")
    build_one.add_argument("--gadm", help="narrow to a GADM subdivision id, such as AUS.11_1")
    build_one.add_argument("--years", help="FROM,TO; default the last ten years")
    build_one.add_argument(
        "--min-records",
        type=int,
        default=checklist.REGULAR,
        help="drop rarer birds as vagrants (default %(default)s, as upstream)",
    )
    build_one.add_argument("--output", type=Path, help="default work/checklists/<region-date>.json")
    build_one.add_argument("--json", action="store_true")
    inventory = commands.add_parser("inventory", help="refresh coverage from a dated checklist")
    inventory.add_argument("--checklist", type=Path, required=True)
    inventory.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="summarize the last inventory and work state")
    status.add_argument("--json", action="store_true")
    listing = commands.add_parser("list", help="list active backlog, ordered by observation count")
    listing.add_argument(
        "--state",
        choices=(
            "missing",
            "researching",
            "sourced",
            "downloaded",
            "cut",
            "needs-review",
            "imported",
            "boxed",
            "approved",
            "ready-pr",
            "submitted",
            "merged",
        ),
    )
    listing.add_argument("--all", action="store_true", help="include covered and inactive records")
    listing.add_argument("--limit", type=positive_limit, default=20)
    listing.add_argument("--json", action="store_true")
    next_bird = commands.add_parser("next", help="next unblocked missing bird (read-only)")
    next_bird.add_argument(
        "--batch", help="restrict to an existing batch; default unassigned backlog"
    )
    next_bird.add_argument("--json", action="store_true")
    show = commands.add_parser("show", help="show a species and its inventory provenance")
    show.add_argument("species")
    show.add_argument("--json", action="store_true")
    batch = commands.add_parser("batch", help="small, immutable artwork batches")
    batches = batch.add_subparsers(dest="batch_command", required=True)
    create = batches.add_parser("create", help="select one to five missing birds")
    create.add_argument("id")
    create.add_argument("species", nargs="+")
    create.add_argument("--json", action="store_true")
    batch_show = batches.add_parser("show")
    batch_show.add_argument("id")
    batch_show.add_argument("--json", action="store_true")
    migrate = commands.add_parser("migrate", help="back up and upgrade the local state schema")
    migrate.add_argument("--json", action="store_true")
    source = commands.add_parser("source", help="record and review source/species/licence evidence")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    record = source_commands.add_parser(
        "record", help="freeze a source record and its original metadata"
    )
    record.add_argument("species")
    record.add_argument("--file", type=Path, required=True)
    record.add_argument("--json", action="store_true")
    source_show = source_commands.add_parser("show", help="inspect source revisions and approval")
    source_show.add_argument("species")
    source_show.add_argument("--json", action="store_true")
    approval = source_commands.add_parser(
        "approve", help="record an explicit human review of one revision"
    )
    approval.add_argument("species")
    approval.add_argument("--revision", type=int, required=True)
    approval.add_argument("--reviewer", required=True)
    approval.add_argument("--notes", required=True)
    approval.add_argument("--confirm-source-species-licence", action="store_true", required=True)
    approval.add_argument("--json", action="store_true")
    block = commands.add_parser("block", help="pause a bird while retaining its current phase")
    block.add_argument("species")
    block.add_argument("--reason", choices=store.BLOCK_REASONS, required=True)
    block.add_argument("--notes", required=True)
    block.add_argument("--json", action="store_true")
    unblock = commands.add_parser(
        "unblock", help="record a resolution and resume the retained phase"
    )
    unblock.add_argument("species")
    unblock.add_argument("--notes", required=True)
    unblock.add_argument("--json", action="store_true")
    fetch_scan = commands.add_parser("fetch", help="acquire a scan after human source approval")
    fetch_scan.add_argument("species")
    fetch_scan.add_argument("--json", action="store_true")
    cut = commands.add_parser("cutout", help="cut the approved scan with upstream's native tools")
    cut.add_argument("species")
    how = cut.add_mutually_exclusive_group(required=True)
    how.add_argument("--spec", type=Path, help="plate.py spec naming the approved scan")
    how.add_argument("--manual", type=Path, help="a hand-edited transparent export")
    cut.add_argument("--notes", default="", help="edit notes; required with --manual")
    cut.add_argument("--json", action="store_true")
    revalidation = commands.add_parser("revalidate", help="fresh dry run of unchanged current cut")
    revalidation.add_argument("species")
    revalidation.add_argument("--json", action="store_true")
    bring = commands.add_parser("import", help="file the reviewed cut upstream, provisionally")
    bring.add_argument("species")
    bring.add_argument("--json", action="store_true")
    replacement = commands.add_parser(
        "replace", help="replace a boxed plate with an explicit cut revision"
    )
    replacement.add_argument("species")
    replacement.add_argument("--candidate", type=Path, required=True)
    replacement.add_argument("--notes", required=True)
    replacement.add_argument("--json", action="store_true")
    box = commands.add_parser("box", help="journal a sitting with upstream's bird box editor")
    box.add_argument("species")
    box.add_argument("--propose", type=float, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    box.add_argument("--notes", default="")
    box.add_argument("--port", type=int, default=8081)
    box.add_argument("--json", action="store_true")
    final_review = commands.add_parser(
        "review", help="native review evidence and explicit human quality approval"
    )
    reviews = final_review.add_subparsers(dest="review_command", required=True)
    for name in ("build", "show", "approve"):
        sub = reviews.add_parser(name)
        sub.add_argument("species")
        sub.add_argument("--json", action="store_true")
        if name == "approve":
            sub.add_argument("--review", type=int, required=True)
            sub.add_argument("--reviewer", required=True)
            sub.add_argument("--notes", required=True)
            sub.add_argument("--confirm-quality", action="store_true", required=True)
    interface = commands.add_parser("ui", help="local page to walk birds through each gate")
    interface.add_argument("--port", type=int, default=8090)
    interface.add_argument("--no-browser", action="store_true")
    interface.add_argument("--json", action="store_true")
    notes = commands.add_parser("feedback", help="open review notes left in fugart ui")
    notes.add_argument("species", nargs="?")
    notes.add_argument("--all", action="store_true", help="include resolved notes")
    notes.add_argument("--resolve", metavar="ID", help="close a note, saying what was done")
    notes.add_argument("--notes", default="")
    notes.add_argument("--by", default="")
    notes.add_argument("--json", action="store_true")
    return command


def execute(args: argparse.Namespace):
    workspace = args.workspace.resolve()
    if args.command == "init":
        return store.initialize(workspace, args.upstream.resolve())
    if args.command == "checklist":
        return checklist.build(
            workspace,
            args.country,
            gadm=args.gadm,
            years=args.years,
            min_records=args.min_records,
            output=args.output and args.output.resolve(),
        )
    if args.command == "inventory":
        return store.refresh(workspace, args.checklist)
    if args.command == "migrate":
        return store.migrate(workspace)
    if args.command == "fetch":
        return fetch.acquire(workspace, args.species)
    if args.command == "cutout":
        return cutout.run(
            workspace,
            args.species,
            args.spec and args.spec.resolve(),
            args.manual and args.manual.resolve(),
            args.notes,
        )
    if args.command == "revalidate":
        return cutout.revalidate(workspace, args.species)
    if args.command == "import":
        return imports.run(workspace, args.species)
    if args.command == "replace":
        return revisions.replace(workspace, args.species, args.candidate.resolve(), args.notes)
    if args.command == "box":
        if args.propose is not None:
            return boxing.propose(workspace, args.species, args.propose, args.notes)
        return boxing.run(workspace, args.species, args.port)
    if args.command == "review" and args.review_command == "build":
        return review.build(workspace, args.species)
    if args.command == "review" and args.review_command == "approve":
        return review.approve(workspace, args.species, args.review, args.reviewer, args.notes)
    if args.command == "source" and args.source_command == "record":
        return sources.record(workspace, args.species, args.file.resolve())
    if args.command == "source" and args.source_command == "approve":
        return sources.approve(workspace, args.species, args.revision, args.reviewer, args.notes)
    if args.command in ("block", "unblock"):
        return sources.block(
            workspace, args.species, args.reason if args.command == "block" else None, args.notes
        )
    if args.command == "ui":
        if not 0 <= args.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        return ui.serve(workspace, args.port, not args.no_browser)
    if args.command == "feedback":
        if args.resolve:
            return ui.resolve_feedback(workspace, args.resolve, args.notes, args.by)
        key = None
        if args.species:
            with store.connect(workspace, readonly=True) as db:
                key = store.find_species(db, args.species)["key"]
        return ui.feedback(workspace, key, open_only=not args.all)
    if args.command == "batch" and args.batch_command == "create":
        return store.create_batch(workspace, args.id, args.species)
    with store.connect(workspace, readonly=True) as db:
        if args.command == "review":
            return review.show(db, workspace, args.species)
        if args.command == "status":
            return store.status(db)
        if args.command == "list":
            return store.list_species(db, state=args.state, all_species=args.all, limit=args.limit)
        if args.command == "next":
            return store.next_species(db, args.batch)
        if args.command == "show":
            return store.show_species(db, args.species) | {
                "source": sources.show(db, args.species),
                "cutout": cutout.show(db, workspace, args.species),
                "import": imports.show(db, args.species),
                "box": boxing.show(db, args.species),
                "review": review.show(db, workspace, args.species),
            }
        if args.command == "source":
            return sources.show(db, args.species)
        return store.show_batch(db, args.id)


def bird_line(bird: dict) -> str:
    phase = (
        f"blocked:{bird['blocked_reason']}"
        if bird["blocked_reason"]
        else bird["state"] or "covered"
    )
    active = "" if bird["active"] else " [inactive]"
    return f"{bird['key']:34} {bird['common_name']:26} {phase}{active}"


def display(command: str, result) -> None:
    if command == "init":
        verb = "Initialized" if result["created"] else "Already initialized"
        print(f"{verb}: {result['database']}\nUpstream: {result['upstream']} (classic)")
    elif command == "checklist":
        print(
            f"{result['detectable']} detectable birds in {result['region']} "
            f"({result['years']}; {result['gbif_names']} GBIF names)"
        )
        print(
            f"Wrote {result['checklist']}\nNext: fugart inventory --checklist {result['checklist']}"
        )
    elif command in ("inventory", "status"):
        print(
            f"{result['total']} species; {result['covered']} covered; {result['missing']} missing"
        )
        if command == "inventory":
            verb = "Refreshed" if result["changed"] else "Unchanged"
            print(f"{verb}: inventory {result['id']} at {result['upstream_revision']}")
        else:
            print(
                f"{result['batches']} batches; states: {json.dumps(result['states'], sort_keys=True)}"
            )
            if result["inventory"]:
                print(f"Inventory time: {result['inventory']['created_at']} (cached coverage)")
    elif command == "list":
        print("\n".join(bird_line(bird) for bird in result) or "No matching birds.")
    elif command == "next":
        print(bird_line(result) if result else "No eligible missing birds.")
    elif command == "batch":
        print(f"Batch {result['id']} ({len(result['members'])} birds; research selection)")
        print("\n".join(bird_line(bird) for bird in result["members"]))
    elif command == "feedback" and isinstance(result, list):
        for note in result:
            state = "resolved" if note["resolution"] else "open"
            print(f"{note['id']}  {note['species']:30} {note['gate']:8} {state:8} {note['by']}")
            print(f"    {note['notes']}")
        print("" if result else "No open feedback.", end="\n" if not result else "")
    elif command == "ui":
        print(f"Stopped {result['url']}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def main() -> None:
    command = parser()
    args = command.parse_args()
    try:
        result = execute(args)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            display(args.command, result)
    except (ValueError, TypeError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        command.exit(1, f"fugart: {error}\n")


if __name__ == "__main__":
    main()
