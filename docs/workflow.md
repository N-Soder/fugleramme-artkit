# Workflow reference

Every command, what it checks and what it leaves behind. The [README](../README.md)
has the short version; [design.md](design.md) has the reasoning. Examples use
Welcome Swallow (`Hirundo neoxena`) from a real batch; any bird in your inventory works.

## Build a checklist

```bash
uv run fugart checklist build --country NZ
uv run fugart checklist build --country AU --gadm AUS.11_1 --years 2017,2026
uv run fugart inventory --checklist work/checklists/nz-2026-10-30.json
```

`checklist build` asks GBIF the same question as upstream's
[coverage page](https://arnegiacomo.dev/fugleramme/species/): human observations of
birds with coordinates in the last ten years, counted per scientific name, keeping
names with at least 25 records (`--min-records`). It then joins those names to the
birds BirdNET can detect, using the checkout's own species rows and old label
spellings, so the result matches what the page shows for that country. GBIF
records are presence data, not detection probabilities or ranges.

The checklist lands in `work/checklists/<code>-<date>.json` (or `--output`). The raw
GBIF responses are kept beside it, and their URLs and SHA-256 hashes are recorded
in the file. If GBIF lists more names than its facet paging allows, the build fails
rather than writing a partial list. `--gadm` narrows to a GADM subdivision id
(GBIF's `gadmGid`), such as `AUS.11_1` for Western Australia. Pass `--years` to pin
the window so a later rebuild asks the same question.

`examples/au-2026-10-05.json` is a checklist built this way for Australia, kept as
a sample of the format.

## State and retries

- `.fugart/state.sqlite3` holds configuration, inventory snapshots, phases, audit
  events, batches, source revisions, human reviews and fetch journals. `work/`
  holds research, immutable evidence, original scans and subsequent artwork scratch.
  Both are ignored by Git. Preserve them when resuming; a fresh clone contains
  the code and checklist, not your local work history.
- `init` is a no-op for the same checkout and refuses silent rebinding. The two
  repositories must be separate directory trees. Only `classic` is supported.
  Read commands never initialize a database.
- Identical inventory reruns are no-ops. Refresh updates coverage and counts
  without resetting work or batch membership. Absent species become inactive.
  Unknown names, incomplete checklists and malformed input fail before writing.
  Cached coverage is not proof that a PR merged.
- Repeating a batch name with the same members is a no-op; changing membership
  fails. Covered, inactive, blocked or assigned birds cannot enter a new batch.
  There is no arbitrary state editing or approval bypass command.
- Inventory calls native helpers with Python bytecode writes disabled. It records
  the Git revision, dirty status and relevant input fingerprints. It does not
  install upstream dependencies or invoke artwork mutation tools.
- All workspace mutations share a nonblocking lock; a competing operation fails
  clearly. Read commands still work while a scan is being fetched.
- State from an older version needs `uv run fugart migrate`. It retains a
  SQLite backup under `.fugart/backups/` before the additive migration to
  schema 4. Fresh workspaces start on schema 4. Unknown versions fail without
  resetting anything.

## Review in the browser

```bash
uv run fugart ui            # http://127.0.0.1:8090/
```

One page per bird, with a step for each gate: source, scan, cut, upstream, bird box, final and
PR. Each step shows the evidence for that gate: the plate and its licence record, the cut
against the scan (hold Space to flip), the automatic checks and the retouching to disclose,
a bird box you can drag, and the plate on the e-ink panel and web page. Approvals call the
same functions as the CLI, with your name, notes and an explicit tick standing in for the
`--confirm` flags. "Send back with this note" queues the note in `work/feedback.jsonl`. An
agent lists the queue with `fugart feedback` and closes each note with
`fugart feedback --resolve ID --notes "what changed" --by NAME`. Nothing is posted upstream
from the page.

The server listens on 127.0.0.1 only, rejects other Host headers, and accepts a POST only
with the token embedded in the page it served.

## Box an imported bird

```bash
uv run fugart box 'Hirundo neoxena' --port 8081
# then open http://127.0.0.1:8081/ in a browser
```

The command journals the exact imported plate and geometry preimage, then runs
upstream's autosaving `bird_box.py`. `BROWSER=true` suppresses its external browser;
the installed Python's browser selection is checked before launch. Open the page
in any browser yourself. Box one whole bird, including bill, tail and feet, following
the native page's instructions. The vignette itself stays intact.

After editing, press Ctrl-C in the command's terminal. A valid saved box against
the native trimmed dimensions advances `imported` to `boxed`. No saved edit leaves
the bird `imported`. A pending operation is reconciled before another editor can
launch. Changed plates, manifests, other boxes or unrelated checkout edits stop
for repair. `show` includes the latest `box` journal. Boxing records geometry,
not a final quality approval.

Imports permit existing uncommitted artwork only when its complete contents match
`HEAD` plus completed import and box journals. Each new import records that
preimage and must preserve it. Paths alone never excuse unrelated changes.

## Record and review a source

Research stays under `work/research/`; the CLI records your findings rather than
scraping or guessing taxonomy. Recording a candidate does not approve it.

```bash
uv run fugart source record 'Hirundo neoxena' \
  --file work/research/hirundo-neoxena-source.json
uv run fugart source show 'Hirundo neoxena'
```

A source input is JSON with these required fields. The example is a real Gould plate
used for Welcome Swallow; swap in your own work, provider and evidence:

```json
{
  "schema_version": 1,
  "scientific_name": "Hirundo neoxena",
  "historical_name": "Hirundo neoxena",
  "work": "The Birds of Australia, volume 2",
  "work_date": "1840-1848",
  "creators": ["John Gould", "Henry Constantine Richter"],
  "provider": "Smithsonian Libraries via BHL / Internet Archive",
  "source_key": "gould",
  "plate_url": "https://archive.org/details/birdsAustraliav2Goul/page/n59/mode/1up",
  "download_url": "https://archive.org/download/birdsAustraliav2Goul/page/n59_s1.jpg",
  "licence": "Public domain",
  "licence_url": "https://commons.wikimedia.org/wiki/File:The_birds_of_Australia_(IA_birdsAustraliav2Goul).pdf",
  "licence_evidence": "Exact scan's Commons record reports Public domain; retain and review the original response.",
  "taxonomy_notes": "Plate caption uses the current scientific name; review the actual bird.",
  "taxonomy_urls": ["https://archive.org/details/birdsAustraliav2Goul/page/n59/mode/1up"],
  "evidence": [
    {"url": "https://archive.org/metadata/birdsAustraliav2Goul", "file": "birdsAustraliav2Goul-metadata.json"},
    {"url": "https://commons.wikimedia.org/wiki/File:The_birds_of_Australia_(IA_birdsAustraliav2Goul).pdf", "file": "birdsAustraliav2Goul-commons-licence.json"}
  ],
  "notes": "Optional research notes; never inferred approval."
}
```

Evidence paths are relative to the source JSON and must exist inside this
workspace. Each file is capped at 8 MiB and copied unchanged to hash-addressed
`work/evidence/`. Include the actual licence and taxonomy responses, not just a
metadata record lacking rights. All source URLs must use credential-free HTTPS.
Input records reject unknown fields. `source record` checks the current scientific
name and returns the immutable revision and evidence fingerprint.

After a human has reviewed the exact plate, species mapping and licence:

```bash
uv run fugart source approve 'Hirundo neoxena' --revision 1 \
  --reviewer NAME --notes 'Reviewed plate, species and licence evidence' \
  --confirm-source-species-licence
uv run fugart fetch 'Hirundo neoxena'
```

The confirmation flag records a human decision; the tool cannot verify that
judgment. An agent must not run approval just because the example exists.
Approval advances `researching` to `sourced`; fetch advances it to `downloaded`.
Identical source/approval reruns are no-ops. Edited source evidence creates a new
revision, returns the bird to `researching` and requires new approval; old
reviews and scans remain. Source edits after cutting are deferred to the later
artifact revision workflow. Inventory name changes require updated source evidence.

Fetch allows an explicit list of scan providers (Internet Archive, Wikimedia image
storage, Project Gutenberg, the Biodiversity Heritage Library and the University of
Kansas digital library; see `SCAN_HOSTS` in `src/fugart/sources.py`), checks every redirect, uses 30-second network
timeouts, caps acquisition at 128 MiB and checks elapsed time against 120 seconds.
It fully decodes JPEG/PNG/WebP/TIFF with Pillow's image-bomb protection and requires
a 600 px longest side. Original bytes stay unchanged under `work/scans/<sha256>`
with a format extension. Source revision, checksum, dimensions, response URL,
headers and acquisition time are retained. Failed attempts leave `sourced` and
keep an audit event. Incomplete attempts restart; a verified interrupted scan
resumes without HTTP. Complete corrupt/missing files fail rather than silently
replacing evidence. Checks run on every retry; no destructive force option exists.

```bash
uv run fugart block 'Chenonetta jubata' --reason taxonomy-ambiguity \
  --notes 'Explain the unresolved mapping and references'
uv run fugart unblock 'Chenonetta jubata' --notes 'Explain the reviewed resolution'
```

Other block reasons are `source-unavailable`, `licensing-uncertainty` and
`artwork-quality`. Blocking preserves the phase and prevents approval/fetch;
unblocking records a resolution without granting approval.

## Cut a downloaded scan

Cutting, finishing and encoding are upstream's: `tools/platereview/plate.py`,
`finish.py` and `tools/add_bird.py --dry-run`. This CLI only checks the inputs,
runs them and records what they wrote. Write a spec as upstream's
`tools/platereview/README.md` describes, with `scan` set to the absolute path
of the bird's downloaded scan (`fugart source show` prints its path under the workspace).
`plate.py` decides paper by one threshold and eats pale drawing, so treat this as a first
cut: the plate that ships is recut with `tools/recut.sh` and filed with `fugart replace`
(see [CUTTING.md](../CUTTING.md)).

```json
{
  "scan": "/absolute/path/to/work/scans/<sha256>.jpg",
  "box": [450, 2450, 2050, 3750],
  "paper_min": 182,
  "seed": [700, 420]
}
```

```bash
uv run fugart cutout 'Hirundo neoxena' --spec work/specs/hirundo-neoxena.json \
  --notes 'What was kept, dropped and corrected'
uv run fugart show 'Hirundo neoxena'
```

Each run writes a new `work/cuts/<key>/rNNN/` holding `spec.json`, the native
`crop.png`, `cut.png`, `check.jpg`, `final.png`, the importer's dry-run
`preview.png` and `log.json` (every native command, its output, any `!`
warnings such as severed pixels, your notes and the colour/cleanup disclosures).
Files are hashed, recorded and made read-only. Fix a fault by editing the spec
and cutting again: that is a new revision and the old one stays. The bird moves
`downloaded` -> `cut` -> `needs-review`. A failed step keeps its log, the earlier
revisions and the phase already reached. `needs-review` means there is something
to look at, not that anyone approved it; quality approval is a later human gate.

A landscape plate bound sideways takes `"rotate": 90` (or 180, 270; degrees
clockwise) in the spec. The cut then runs on a lossless upright `scan.png` written
into the revision folder, and `box` and every other coordinate are in that copy's
pixels. The approved scan itself is never rewritten.

The first cut builds upstream's locked environment in `.fugart/upstream-env/`
(needs network once). Nothing is written to the checkout, and a native command
that changes its Git status stops the run. The original scan is only read.

A cut-out edited by hand is accepted with notes; it skips `plate.py`/`finish.py`
and goes straight to the native dry run:

```bash
uv run fugart cutout 'Hirundo neoxena' --manual ~/Desktop/swallow.png \
  --notes 'Cut in Krita from the approved scan; halo added per adding-artwork.md'
```

## Import a reviewed cut

```bash
uv run fugart import 'Hirundo neoxena'
uv run fugart show 'Hirundo neoxena'
```

Import writes the plate and manifest into the Fugleramme checkout; boxing also
writes its geometry. Import runs
upstream's `tools/add_bird.py` on the current cut revision with
`--no-detect --no-box`, which adds `birds/<key>.webp` and one `manifest.json`
entry, and moves the bird `needs-review` -> `imported`. Nothing is committed,
branched or pushed there. The import is provisional: boxing and the human
quality approval still follow.

It refuses unless the bird is unblocked at `needs-review`, the cut's files
still match their recorded hashes, existing uncommitted artwork matches completed
operation journals exactly, and its `HEAD` matches either the cut's dry run or a fresh intact revalidation.

`add_bird.py` numbers a new variant on every call, so an import is never
retried blind. The attempt is journaled before the tool runs and the checkout
is read afterwards. If nothing was written, run `import` again. If the result
is partial or unexpected (a plate without its manifest entry, other changed
files), the command stops and says what it found: repair the checkout by hand,
either completing the import or reverting it, and run `import` again. It
re-reads the checkout and only runs the tool once no trace of the attempt is left.

## Checklist format

`inventory --checklist PATH` accepts JSON:

```json
{
  "schema_version": 1,
  "source_url": "https://arnegiacomo.dev/fugleramme/species/",
  "retrieved_at": "2026-10-05T00:00:00+00:00",
  "region": "New Zealand",
  "scope": "Hand-picked subset, with selection rationale recorded separately",
  "complete": true,
  "species": [{"scientific_name": "Hirundo neoxena", "records": 960164}]
}
```

`fugart checklist build` writes this format for you. `region` is optional and only
labels the review page. `complete` means complete for the declared scope; a truncated source response
must not use it. Names must be detectable birds recognized by native helpers.
`records` is optional, defaults to zero and must be a nonnegative integer.
Aliases fold into one species and their counts sum. Additional query URLs,
source hashes and notes are retained in SQLite and exposed by `show`.
Plate/licence provenance uses the separate source records described above.

## Checks

```bash
uv run pytest -q
FUGART_TEST_UPSTREAM=/absolute/path/to/fugleramme uv run pytest -q
uv run ruff check
uv run ruff format --check
uv build
```

The first command runs offline tests and skips the explicit real-checkout smoke
tests. The second also checks the example checklist against a real checkout
and tests inventory retries. Native tool behavior remains upstream's test suite.
Offline source/fetch tests cover approval invalidation, evidence corruption,
redirect/byte/time limits, immutable scans, failed attempts and recovery before
and after file promotion. Test human approvals exist only in disposable fixtures.
Cutout tests run stand-in native tools through `uv run --locked` in the fixture
checkout, so they need `uv` but no network; they cover revisions, failures at
each step, manual exports, retained warnings and an untouched checkout.

## Final quality review

After boxing, build fresh native comparison and web/panel previews:

```bash
uv run fugart review build 'Welcome Swallow'
uv run fugart review show 'Welcome Swallow' --json
```

The build returns an operation id and a unique folder under `work/review/`.
Serve that folder with a localhost HTTP server and inspect `index.html`,
`preview/web.png` and `preview/panel.png` in a browser. This does not open one for you.
Native browser Pass marks are feedback only. After explicitly reviewing those
exact files, record the human gate (replace the example id with the build id):

```bash
uv run fugart review approve 'Welcome Swallow' --review 4 --reviewer NAME \
  --notes 'Reviewed comparison and web/panel previews; clean.' --confirm-quality
```

Build and approval use schema 4's existing operation journal (`review-build` and
`quality-review` kinds); no migration is required. Every approval retains the
human identity, notes and exact evidence fingerprint. A new build requires a new
approval. Source, cut, plate, manifest entry, box, checkout revision or review-file
changes make the approval stale. `show`, `status`, `list` and batch output then
report `boxed`; the old approval remains audit history. These read commands do
not rewrite the database phase. Any future validation/PR gate must verify current
review evidence, rather than trusting the stored phase alone.

Failed or interrupted builds never count as review evidence. Run build again to
create a fresh operation/folder; retained failed folders are not overwritten.
Manual cuts currently need matching crop/cut comparison evidence before final
review; native spec cuts have both.

## Revising and revalidating existing work

If the checkout moved since a cut's dry run, validate the unchanged current cut
with the current native tools before importing it:

```bash
uv run fugart revalidate 'Black Swan'
uv run fugart import 'Black Swan'
```

Revalidation records a new operation with the current HEAD, source fingerprint,
cut revision, final-image hash and preview hashes. It preserves the old cut and
its original log. Import accepts this evidence only while those inputs remain intact.

For an already boxed bird, replace the existing asset explicitly rather than
rerunning the importer (which would create a numbered variant):

```bash
uv run fugart replace 'Welcome Swallow' \
  --candidate work/experiments/swallow-compact-pair/v005 \
  --notes 'Compact pair/nest selected; perch contour restored from original plate.'
```

The candidate must provide matching `crop.png`, RGBA `cut.png` in that crop's
coordinates, and `final.png`. The scan crop must match the previous revision.
Replacement creates an immutable cut revision, prepares the WebP with native
helpers, backs up the previous asset and geometry, and journals the intended
writes before updating the existing filename. It projects the previous human
bird box through the original crop coordinates and retains that method in the
review evidence. Old quality approvals become stale. A retry reconciles the
pending replacement, refusing conflicting edits instead of creating another cut.

An agent may propose a box for an imported plate for the human to inspect:

```bash
uv run fugart box 'Black Swan' --propose 0.035 0.015 0.945 0.79 \
  --notes 'Primary swan including bill and tail; excludes water reflection.'
```

The journal and final review explicitly identify these as agent-proposed boxes.
They are geometry for review, never human quality approvals. Build a fresh final
review and inspect its artwork, box and web/panel previews before recording sign-off.
