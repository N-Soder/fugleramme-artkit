# fugleramme-artkit

Add the missing birds to [Fugleramme](https://github.com/arnegiacomo/fugleramme), for any
country.

Fugleramme is a bird frame: it listens with BirdNET and shows the birds it hears as hand-cut
1800s natural-history plates. A bird with no plate shows no art. Its
[coverage page](https://arnegiacomo.dev/fugleramme/species/) lists, country by country,
which birds still need one. `fugart` is a local command-line tool that takes those birds,
one small batch at a time, from "no art" to a reviewed plate sitting in your Fugleramme
checkout, ready for a PR.

It keeps the slow, careful parts careful: every source and licence is recorded with its
evidence, a person approves each source and each finished plate, and every cut is kept as an
immutable revision. Upstream's own tools do the cutting, encoding and rendering.

> An independent companion tool. It is not part of, or endorsed by, the Fugleramme project.
> Follow upstream's [contributing guide](https://github.com/arnegiacomo/fugleramme/blob/main/CONTRIBUTING.md)
> for anything you send there.

## How it works

```text
checklist build ─▶ inventory ─▶ batch create
                                    │
   ┌────────────────────────────────┘
   ▼
source record ─▶ ✋ source approve ─▶ fetch ─▶ cutout ─▶ import ─▶ box ─▶ review build ─▶ ✋ review approve
                  (a person)                                                               (a person)
```

| Step | What happens |
| --- | --- |
| `checklist build` | Asks GBIF which birds are regularly seen in a country (or one of its states) and keeps the ones BirdNET can hear. Same query as the coverage page. |
| `inventory` | Compares that list with your checkout's artwork: which are covered, which are missing. |
| `batch create` | Picks one to five missing birds to work on. |
| `source record` | Records a plate, its licence and taxonomy evidence. Evidence files are hashed and kept. |
| `source approve` ✋ | A person confirms the plate, the species and the licence. |
| `fetch` | Downloads the scan from an approved provider and verifies it. |
| `cutout` | Cuts the bird out with upstream's tools. Each run is a new, read-only revision. |
| `import` | Files the plate in your checkout with upstream's importer. Nothing is committed. |
| `box` | Sets the bird box in upstream's editor. |
| `review build` / `approve` ✋ | Builds upstream's comparison and e-ink previews; a person signs off. |

`fugart ui` puts all of this on one local page per bird, with the evidence for each gate
and buttons for the human decisions.

**Not built yet:** batch validation and PR preparation. For now you run upstream's tests and
open the PR yourself.

## Requirements

- macOS or Linux
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Git, and a clone of Fugleramme (your fork, if you plan to open PRs) in a separate folder
- Network access for GBIF, scan downloads, and building upstream's environment on the
  first cut

Tested against Fugleramme v0.28.1 (`2cf70e5`). A weekly CI job checks the current upstream.

## Quick start

```bash
git clone https://github.com/arnegiacomo/fugleramme ~/src/fugleramme
git clone https://github.com/N-Soder/fugleramme-artkit ~/src/fugleramme-artkit
cd ~/src/fugleramme-artkit
uv sync --locked

uv run fugart init --upstream ~/src/fugleramme
uv run fugart checklist build --country NZ        # any two-letter ISO country code
uv run fugart inventory --checklist work/checklists/nz-<date>.json
uv run fugart status
uv run fugart list --state missing --limit 10
```

The first build for New Zealand finds about 167 birds BirdNET can hear, 89 of them with no
classic plate. Pick a few and start a batch:

```bash
uv run fugart batch create nz-01 'Tui' 'New Zealand Fantail' 'Silvereye'
uv run fugart next --batch nz-01
uv run fugart ui                                   # http://127.0.0.1:8090/
```

Birds can be named by common name, scientific name or BirdNET label. From here,
[docs/workflow.md](docs/workflow.md) walks through each command: the source record format,
fetching, cutting, importing, boxing and final review.

To narrow to a state or region, pass a GADM id: `--country AU --gadm AUS.11_1` is Western
Australia. `--years 2017,2026` pins the ten-year window so a rebuild asks the same question.

## Working with an AI agent

Built and tested with [Claude Code](https://claude.com/claude-code) running Claude Opus 5.5.
It isn't tied to Claude: any coding agent that reads [AGENTS.md](AGENTS.md) and can run shell
commands should manage it (Codex, Cursor, Gemini CLI and similar). `CLAUDE.md` just points
at `AGENTS.md`.

The split that works:

| The agent | You |
| --- | --- |
| Finds candidate plates and gathers licence and taxonomy evidence | Approve each source (`source approve` or the UI) |
| Records sources, fetches scans, writes cut specs, recuts | Look at the cuts in `fugart ui` and send notes back |
| Runs the QA checks, works through your notes (`fugart feedback`) | Accept the bird box |
| Builds the final review | Give final sign-off, then open the PR |

Try prompts like:

- "Read AGENTS.md. Build a checklist for Ireland, inventory it, and suggest a first batch of
  three common birds with no art that have good public-domain plates."
- "Research sources for batch ie-01 and record them. Stop when they're ready for me to approve."
- "Work through the open feedback in `fugart feedback`."

The tool enforces the important boundaries itself. Approvals need a reviewer name, notes and
an explicit `--confirm-*` flag. The review page only accepts posts carrying the token from the
page it served. Agent-proposed boxes are labelled as such. Nothing is ever posted upstream.
`AGENTS.md` tells agents never to approve on a person's behalf and never to post upstream
unasked.

## Cutting well

Read [CUTTING.md](CUTTING.md) before your first cut. In short: keep the whole drawn vignette,
retone the page rather than cutting into it, hold colour to the scan, and choose plates drawn
on white paper where you can. `tools/recut.sh` runs the vignette cutter, upstream's finisher,
the colour hold and `tools/qa.py`, which fails a cut for the faults upstream has sent back
before.

Upstream's rules on retouching: conventional tools only. A generative model may fill only a
small, masked spot of damage, and the PR must say so. Keep PRs to one to three birds, and
claim the species in an upstream issue first.

## Your local state

`fugart` keeps everything in the folder you run it from:

- `.fugart/state.sqlite3`: inventory, batches, sources, approvals, the audit trail
- `work/`: checklists, research, evidence, scans, cut revisions and review pages

Both are ignored by Git and hold the only copy of your history. Back them up, and copy them
across when you move machines. A fresh clone has the code, not your work.

## Docs

- [docs/workflow.md](docs/workflow.md): every command, what it checks, and how to recover
- [docs/design.md](docs/design.md): state model, upstream contracts, failure handling
- [CUTTING.md](CUTTING.md): what a cut must get right, and what enforces each rule
- [AGENTS.md](AGENTS.md): instructions for AI agents
- [CONTRIBUTING.md](CONTRIBUTING.md): working on the tool itself

## Development

```bash
uv sync --locked
uv run pytest -q                                   # offline; stand-in upstream tools
FUGART_TEST_UPSTREAM=/absolute/path/to/fugleramme uv run pytest -q
uv run ruff check && uv run ruff format --check
uv build
```

## License

MIT; see [LICENSE](LICENSE). Scans and artwork are not part of this repository and belong to
their sources. Fugleramme's artwork is CC BY-SA 4.0; see upstream for its terms.
