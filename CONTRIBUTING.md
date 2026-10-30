# Contributing

Thanks for helping. This file is about the tool itself. Artwork goes to
[Fugleramme](https://github.com/arnegiacomo/fugleramme) under its own contributing guide.

## Setup

```bash
uv sync --locked
uv run pytest -q
```

The suite is offline. It builds a small stand-in Fugleramme checkout per test (see
`tests/conftest.py`), so it needs `uv` and Git but no network. To also check against a real
checkout:

```bash
FUGART_TEST_UPSTREAM=/absolute/path/to/fugleramme uv run pytest -q
```

## Before you open a PR

- `uv run pytest -q`, `uv run ruff check`, `uv run ruff format --check` and `uv build` pass.
- New behaviour has a test. A fix has a test that failed before it.
- Docs describe how things work now: update `docs/workflow.md` for a command change and
  `CUTTING.md` for a cutting rule.

## What fits

- Fixes, and support for new upstream versions.
- A new scan provider in `SCAN_HOSTS` (`src/fugart/sources.py`), with a link showing
  upstream has accepted plates from it.
- New `tools/qa.py` checks for faults upstream review has sent back, each with a test.
- Batch validation and PR preparation (see `docs/design.md`).

What doesn't fit: commands that set a phase or skip a human gate, reimplementing upstream's
taxonomy or image tools, or anything that posts to upstream on its own.

## Agents

PRs written with an AI agent are welcome. Point it at [AGENTS.md](AGENTS.md), and review
what it wrote before you open the PR.
