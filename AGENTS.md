# Agent notes

Instructions for any coding agent working in this repo or driving `fugart` for a person.
Built and tested with Claude Code on Claude Opus 5.5; nothing here is specific to it.

## What this is

A local CLI (`fugart`) that takes birds with no artwork in a separate
[Fugleramme](https://github.com/arnegiacomo/fugleramme) checkout to reviewed plates in that
checkout. Read [README.md](README.md) first, then [docs/workflow.md](docs/workflow.md) for
the command you are about to run.

## Rules that are not optional

- **Humans approve; agents prepare.** Never run `source approve` or `review approve`, or
  tick an approval in `fugart ui`, unless the person has looked at that exact evidence and
  told you to in this session. `--reviewer` is their name, never yours. The `--confirm-*`
  flags record a human decision; passing one on your own judgment falsifies the record.
- **Nothing goes upstream without an explicit instruction.** No issues, PRs, comments or
  review requests on the Fugleramme repo unless the person asks for that specific post.
- **Never commit, branch or push in the Fugleramme checkout** unless asked. `fugart import`,
  `box` and `replace` write files there on purpose; leave them uncommitted.
- **Upstream's artwork rules win.** Real plates only. Retouching with conventional tools is
  fine; a generative model may only fill a small masked spot of damage, disclosed in the PR
  (upstream `CONTRIBUTING.md`). Never generate or redraw bird artwork.
- **Before writing a cut spec, recutting or showing anyone a cut, read
  [CUTTING.md](CUTTING.md).** It holds the rules upstream's maintainer taught in review.
  When a review teaches a new rule, add it there and, where possible, to `tools/qa.py`.
- **No state shortcuts.** There is no command to set a phase or bypass a gate, and none
  should be added. Do not edit `.fugart/state.sqlite3` by hand.
- **Local state is precious.** `.fugart/` and `work/` hold the only copy of approvals,
  evidence, scans and cut history. Never delete them. They are ignored by Git.

## Typical session

1. `uv run fugart status` and `uv run fugart feedback` to see where things stand and
   what the person sent back from the review page.
2. Research a source for a bird: find a plate, record licence and taxonomy evidence in
   `work/research/`, then `fugart source record`. Stop and ask the person to approve.
3. After approval: `fugart fetch`, write a spec, cut (`fugart cutout`, or `tools/recut.sh`
   then `fugart replace`). Look at full-size close-ups yourself before showing anyone.
4. `fugart import`, propose a box with `fugart box --propose` if asked, `fugart review build`.
   Point the person at `fugart ui` for the final decision.
5. Resolve each feedback note with `fugart feedback --resolve ID --notes "what changed"`.

## Development

- Python 3.12+, `uv`, standard library plus Pillow. No new dependencies without a reason.
- Upstream crops, finishes, encodes and renders through its own tools; this project cuts
  with `tools/vignette.py`, orchestrates and records. Do not reimplement upstream's taxonomy
  or image tools.
- Validation, all of which must pass before a commit:
  `uv run pytest -q`, `uv run ruff check`, `uv run ruff format --check`, `uv build`.
  With a real checkout: `FUGART_TEST_UPSTREAM=/absolute/path/to/fugleramme uv run pytest -q`.
- Tests use stand-in upstream tools (see `tests/conftest.py`) and never touch the network.
- Document what is there, not how it changed. Keep the language plain.
