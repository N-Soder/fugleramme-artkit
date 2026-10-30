# Design

## Purpose and scope

A local, single-operator production line around a separate Fugleramme checkout.
It takes a bird that upstream has no artwork for to a reviewed plate in that
checkout: dated backlog, source research with retained evidence, an explicit human
source approval, scan acquisition, native cuts, provisional import, boxing and an
explicit human quality approval. Upstream's own tools do the cutting, encoding and
rendering. Batch validation and PR preparation are planned, not built. Hardware and
deployment are upstream's concern.

## Upstream contracts

Found by reading upstream at `d3b3a20` (2026-10-05) and rechecked at `2cf70e5`
(v0.28.1). These are upstream's contracts; this project uses them rather than
replacing them:

| Evidence in upstream | Consequence |
| --- | --- |
| `docs/adding-artwork.md`, `CONTRIBUTING.md`, `.agents/artwork.md` | Real plates only; AI retouching is permitted and disclosed. Keep PRs around five species. |
| `src/fugleramme/names.py`, `assets/birdnet_aliases.json`, `hooks/species.py` | Frozen BirdNET labels and current canonical names differ. Reuse canonicalization, bird filtering and coverage rows; store both names. |
| `assets/artwork/classic/ATTRIBUTION.md` | Each source has a manifest key (for example `gould`). Reuse an existing key where the work is already credited; a new work needs a new key and attribution entry upstream. Check terms for each actual scan. |
| `tools/add_bird.py` | Owns cropping, 1200 px cap, WebP encoding, variant naming and manifest writes. It appends a new variant on every invocation: blindly retrying is unsafe. |
| `tools/bird_box.py`, `tools/bird_boxes.py` | Native box editor autosaves `geometry.json`; detector is optional and downloads about 2 GB. Use manual boxes first. |
| `tools/platereview/` and `.claude/skills/plate-review/SKILL.md` | Already supplies deterministic spec-based cutting, finishing and scan/render comparison. Scratch paths may live outside the checkout. Browser verdicts are local storage, not durable workflow approvals. |
| `tests/test_artwork_names.py`, `tests/test_artwork_duplicates.py` | Enforces canonical filenames, body mass, attribution, bidirectional manifest/file consistency, boxes, matching trimmed dimensions, minimum 600 px long side and duplicate-image detection. |
| `tools/preview_plates.py`, `.github/workflows/artwork-preview.yml` | Already renders `web.png` and dithered `panel.png`, including previews on artwork PRs. Producing this panel preview is artwork QA, not hardware work. |
| `docs/assets/species.js` | A country is a GBIF occurrence filter, not a BirdNET country list. Default excludes species with fewer than 25 observations per name facet in the current ten-year interval. `fugart checklist build` copies this query. |

Some upstream prose is stale: contribution text still mentions exact frozen
label filenames, and the plate-review skill describes an old resize defect.
The current name helpers, importer and tests are the contracts. Do not modify
upstream docs as part of this companion project.

## Architecture

Python 3.12+, `uv`, standard-library `argparse`, `sqlite3`, `subprocess` and
`pathlib`, with Pillow for verifying downloaded raster scans; pytest and Ruff for
development. POSIX file locking targets macOS/Linux. Argparse avoids a CLI dependency
for this small fixed command set. No web application, taxonomy service, scraping
framework, plugin system, job queue or ORM.

- `src/fugart/cli.py`: CLI parsing, readable output and JSON output.
- `src/fugart/store.py`: SQLite transactions and workflow operations.
- `src/fugart/upstream.py`: a small checkout adapter calling native helpers/tools.
- `src/fugart/sources.py`: immutable evidence and explicit human source reviews.
- `src/fugart/fetch.py`: approved scan acquisition and its recovery journal.
- `src/fugart/cutout.py`: cut revisions, native-command logs and dry-run previews.
- `tests/`: offline state and CLI tests, plus an explicit real-checkout smoke test.
- `src/fugart/checklist.py`: dated GBIF checklists for any country.
- `src/fugart/ui.py`, `ui.html`: the local review app.
- `tools/`: the vignette cutter, colour hold and pre-flight QA (see `CUTTING.md`).
- `examples/`: a sample checklist.
- `.fugart/state.sqlite3`: local config, inventory snapshots and durable state.
- `.fugart/upstream-env/`: upstream's locked `uv` environment, built on first cut.
- `work/`: scans, research notes, immutable cut revisions, review pages, logs and
  PR preparation artifacts. Both local directories are ignored by Git.

`init --upstream PATH` records an absolute Git checkout path and `classic` style
in SQLite. The two repositories must be disjoint. Do not change either Git
branch implicitly. Inventory invokes `hooks/species.rows()` and
`fugleramme.names.normalize()` in a separate Python process pointed at that
checkout. This keeps upstream imports out of this package and avoids recreating
its taxonomy rules. Inventory needs no upstream runtime installation; the
art tools run with upstream's locked `uv` environment and repository as cwd.
`UV_PROJECT_ENVIRONMENT` keeps that environment in this workspace and bytecode
writes are disabled, so running a native tool leaves nothing in the checkout.

## State and evidence

Normal phases:

`missing -> researching -> sourced -> downloaded -> cut -> needs-review -> imported -> boxed -> approved -> ready-pr -> submitted -> merged`

`cut` means the native cutting operation completed; `needs-review` means the
finished cut and its dry-run render are available. `sourced` requires human
source approval. Final quality approval follows import, box adjustment and native
render comparison: an imported asset is provisional until approved.

Blocking is an orthogonal `blocked_reason`, displayed as `blocked:<reason>`
with the retained phase. Reasons: `source-unavailable`, `taxonomy-ambiguity`,
`licensing-uncertainty`, `artwork-quality`. Blocking and unblocking record notes
and events; unblocking resumes the retained phase. `next` skips blocked birds.
This avoids four copies of every phase and preserves the recovery point.

Covered species have no workflow phase until they need backfill. Coverage is an
inventory observation, never evidence of approval or merger. Refreshes preserve
phases, approvals, history and batch membership. A species removed from a new
checklist becomes inactive, not deleted. Identity changes require explicit
reconciliation rather than moving historical work automatically.

## Data model

Schema version is explicit; unknown versions fail.

| Table | Fields and role |
| --- | --- |
| `meta` | Schema version, upstream path, style. |
| `inventory_runs` | Checklist JSON and SHA-256, UTC time, upstream Git revision, dirty flag, relevant-input fingerprint, totals. A unique input fingerprint makes identical reruns a no-op. |
| `species` | Canonical key PK, current scientific name, label scientific names, common name, occurrence count, observed artwork paths, phase, blocked reason/notes, active flag, last inventory run. |
| `batches`, `batch_members` | Named immutable membership, maximum five species, upstream baseline revision, later PR URL and bundle revision. One bird belongs to at most one batch. |
| `events` | Append-only local audit: bird/batch, action, UTC timestamp, notes/evidence reference. |
| `sources` | Bird, immutable revision, fingerprint and JSON holding historical name, plate/download URLs, work, creators, date, provider, licence, retained evidence hashes, taxonomy rationale/references. Added in schema 2. |
| `source_reviews` | One append-only human approval per source revision; reviewer, notes, UTC time and evidence fingerprint. Added in schema 2. |
| `downloads`, `fetch_jobs` | Verified original scan/HTTP provenance per source revision, plus started/failed/verified/complete acquisition journal. Added in schema 2. |
| `artifacts` | One row per file of a cut revision: bird, revision, kind (`spec`, `crop`, `cut`, `check`, `final`, `manual`, `preview`, `log`, `aux`), workspace path, SHA-256, source revision, upstream Git revision and the native argv that wrote it. Output, warnings, notes and disclosures are in the revision's hashed `log.json`. Added in schema 3. |
| `operations` | Journal of native operations that write into the checkout: bird, kind (`import`, `box`, `replace`, `revalidate`, `review-build`, `quality-review`), state (`started`, `complete`, `failed`) and JSON holding the cut revision, image hash, source revision, upstream revision, expected plate path, dimensions and manifest entry, native argv/output and the observed outcome with result hashes. Boxing also retains the geometry preimage, native trimmed dimensions and saved entry. Added in schema 4. |
| `quality-review` operations | Final quality gate in schema 4 operations, human name, UTC time, approval notes and full review-record fingerprint. Read-time validation invalidates stale approvals without erasing history. Source gate uses `source_reviews`. |

Original scans and original metadata are retained. `manifest.json` carries the
upstream-required `source` and original **plate page** URL; the fuller provenance
travels in the local batch bundle. Never put scans or workflow metadata upstream.

## CLI surface

All commands accept a global `--workspace PATH` (default current directory) and
`--json`. Exact canonical key, scientific name, common name or recorded label
identifies a bird; ambiguous identifiers fail with choices rather than selecting a
fuzzy match. [workflow.md](workflow.md) documents every command.

Planned commands, built only once their evidence and gates exist:
`validate --batch ID` and `pr prepare --batch ID`. There is no arbitrary
`set-state` shortcut. Submission and merging are explicit recorded observations
of a PR URL and revision; `pr prepare` will neither push nor open a PR.

## Native integrations and review gates

1. Record a candidate and taxonomy/licence evidence. A human approves that exact
   source revision before scan acquisition. Prefer original scans of works
   already credited upstream; do not infer licence from the work's age.
2. Download into `work/scans/<sha256>.<ext>`; retain response URL, content type,
   checksum and acquisition time. Timeouts/partial data never mark downloaded.
3. Use native `plate.py crop SPEC`, `plate.py cut SPEC`, then
   `finish.py cut.png final.png`, each under a new revision directory. Specs use
   absolute scan paths. Preserve crop-sized `crop.png` and `cut.png` for review.
   Manual transparent exports are also accepted with original scan and edit notes.
4. Run native `add_bird.py final.png --style classic --key CANONICAL_KEY
   --source SOURCE_KEY --url PLATE_PAGE --preview WORK_PREVIEW --dry-run`. `--key`
   avoids an interactive fuzzy search, but is allowed only after native label
   validation. After human source approval and cut completion, import with the
   same arguments minus `--dry-run`, plus `--no-detect --no-box`.
5. Run native `bird_box.py` on the explicit batch files; humans adjust and save
   boxes. Suppress its automatic external browser opening with `BROWSER=true`
   and verify this works on the installed Python; open the page in a browser manually.
   Do not use upstream `review.sh`, which calls macOS `open`. Serve its native
   generated review folder with localhost `python -m http.server` and a browser.
6. Run native `build.py WORK_REVIEW` with `plates.json` naming the actual imported
   plate and the matching scratch crop/cut, then `preview_plates.py WORK_OUT FILES`.
   Human final approval records exported review notes against the exact asset,
   box, provenance and native previews. Browser Pass alone does not advance state.
7. Run upstream artwork tests and the contributor checks before preparing a PR.
   Body-mass gaps need a documented upstream-supported value, not the renderer's
   median fallback. Never add test exceptions to get a plate through validation.
8. Write a local bundle with explicit asset paths, attribution edits if needed,
   baseline/current Git revisions, provenance, approvals, tool logs, test results,
   previews and a short `chore(assets): add ...` PR title/body. Assert the diff
   contains only intended artwork files/manifest/geometry/necessary attribution.

## Failure and recovery

Schema 2 adds sources, source reviews, downloads and a fetch journal. Existing
schema-1 workspaces remain readable; `fugart migrate` makes a SQLite backup before
the additive transactional migration. Fresh workspaces initialize schema 2.
All workspace mutations hold a nonblocking POSIX lock; reads remain available
during acquisition. Source JSON records include local original metadata
files; recording copies them into immutable, content-addressed evidence storage.
Both source revisions and reviews are append-only. Approval names an explicit
revision and confirms source/species/licence correctness; modified metadata or
evidence requires another revision. Inventory scientific-name changes also require
updated source evidence. Source changes retain old scans and approvals
but return pre-cut birds to researching. Editing a cut/imported bird's source is
deferred to the later revision/review workflow, rather than silently resetting it.

Fetch permits HTTPS scan URLs on an explicit provider allowlist, checks
every redirect, caps bytes and elapsed time, verifies/decodes JPEG/PNG/WebP with
Pillow's image-bomb protection enabled, and requires a 600 px longest side.
It retains original bytes without re-encoding. A journal precedes acquisition;
validated metadata is persisted before atomic promotion to a content-addressed
path. A verified interrupted acquisition resumes without another request.
Incomplete downloads restart explicitly; complete missing/corrupt files fail
instead of silently fetching replacement bytes. New attempts never overwrite
completed evidence. Cached success is checked against the source approval and
local checksums on every retry.

SQLite writes use foreign keys and transactions; CLI errors are nonzero and do
not expose a partial inventory or batch. Initialization is idempotent for the
same checkout/style and refuses silent rebinding. Database files are never reset.
Repeat inventory uses content fingerprints, retains the input snapshot in SQLite
and never resets progressed species. Invalid/unknown checklist names fail before
any write. Read commands do not create missing state.

Schema 3 adds `artifacts`; `fugart migrate` backs up and upgrades schema 1 or 2.
`cutout` needs the approved, checksum-verified scan and a spec whose `scan` is
that file's absolute path. Each run takes the next `work/cuts/<key>/rNNN/`
folder; a number is never reused, including after a failed or interrupted run.
The native argv, output and exit status are logged after every command. Recorded
files are write-protected and re-hashed by `show`. Completing crop, cut and finish
records the revision and phase `cut`; a successful native dry run then records
the preview and `needs-review`. A failed step records an event and keeps the
log, the earlier revisions and the phase reached so far. A native command that
changes the checkout's Git status stops the run. No quality verdict is recorded.

Schema 4 adds `operations`; `fugart migrate` backs up and upgrades any earlier
schema. `import` needs an unblocked bird at `needs-review`, its current cut
revision intact and made from the approved source revision, a checkout whose
uncommitted contents match `HEAD` plus completed import/box journals,
the same `HEAD` the cut's dry run ran against, and a dry
run that filed the bird as its first variant. It journals the expected plate
path, dimensions and manifest entry, commits that row, then runs `add_bird.py`
once. The checkout is then read: no change closes the attempt as failed; the
new plate at the reviewed size plus exactly its manifest entry over `HEAD`
records `imported`; previously journaled changes must also be preserved. Anything
else keeps the attempt open and stops for repair.
An open attempt is always re-read before anything else and `add_bird.py` runs
again only after the checkout shows no trace of it.

`box` requires an intact, provisionally imported plate and current approved source
and cut. It journals the current upstream revision, plate/manifest hashes,
existing dirty-file hashes, geometry preimage and native trimmed dimensions before
launching `bird_box.py` with `BROWSER=true`. The operator opens it in a browser, edits and ends
the session with Ctrl-C. Exactly one valid geometry entry advances to `boxed`;
no edit closes the attempt as failed without advancing. Conflicts remain pending
for repair; interrupted autosaves reconcile before relaunch. No quality verdict
is recorded by boxing.

Later filesystem/native-tool operations require an exclusive workspace lock and
an operation journal written **before** execution: expected paths, hashes and
upstream preimage. A crash after `add_bird` must reconcile the expected file,
manifest and geometry before retrying; conflicting or partial results stop for
repair. Never retry by creating another variant or deleting the partial file.
Keep old cut revisions. Explicit recovery reuses verified outputs or starts a
new revision; no destructive `--force` default. Upstream version drift requires
capability checks and revalidation. Dirty unrelated upstream changes block
imports and PR preparation; read-only inventory may report a dirty checkout.

## Testing

Use pytest with temporary SQLite workspaces and small Git checkout fixtures.
Test retries, rollback on invalid input, canonical aliases, coverage by style,
inactive records, preserved phases, schema rejection, exact/ambiguous selection,
batch caps, duplicate ownership and unchanged upstream files. Invoke the real
CLI in integration tests. Native tool behavior remains upstream's test suite.
Later test interrupted import reconciliation and invalidated review evidence,
then run a real batch through both human gates in a browser.

Commands: `uv run pytest -q`, `uv run ruff check`, `uv run ruff format --check`,
`uv build`. Always parameterize SQL and use subprocess argument arrays, preserve
user work, and verify the checkout contract. Ask for human source and quality
decisions when evidence is concrete. Never generate bird artwork, auto-approve,
reimplement cutting/encoding/rendering, or publish without an explicit request.

## Final review

`review build SPECIES` creates a unique native review folder and native web/panel
previews, journaling `review-build` before generation. Complete builds retain
input snapshots, commands and hashes for every output; failed/interrupted builds
are never approvable. `review approve SPECIES --review ID --reviewer NAME
--notes TEXT --confirm-quality` records an explicit human verdict against the
latest complete, intact build. `review show` reports validity and approval.

Final review reuses schema 4's operations journal instead of adding the proposed
reviews table: the existing append-only rows retain identity, time and evidence.
Completed build rows are not edited. Approvals are separate `quality-review`
rows. A fresh build requires fresh approval. Reads report a stale stored
`approved` phase as `boxed`; history and the stored phase remain unchanged. Future
PR gates must validate current evidence rather than the raw species state.
Manual cut comparisons remain unsupported without matching crop/cut inputs.

## Cut replacement and fresh validation

`revalidate SPECIES` binds a fresh native dry run to unchanged cut bytes and the
current HEAD, preserving immutable historical logs. `replace SPECIES --candidate
FOLDER --notes TEXT` preserves a boxed plate's old asset/geometry and records a
new cut, then writes the existing filename with native preparation helpers. A
started replacement blocks new mutations until reconciled; retries accept only
the journaled before/after state. Source scan/crop identity and all cut hashes
remain checked. Its prior human box is reprojected through unchanged scan-crop
coordinates, and the method is explicit in the fresh review fingerprint.

`box --propose LEFT TOP RIGHT BOTTOM --notes TEXT` records agent-proposed geometry
for final human review. It does not claim a human edited the box or approve art.
Human quality approval still names the exact latest intact review operation.
