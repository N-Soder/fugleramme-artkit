# Cutting rules

What a cut has to get right before it goes upstream. Every rule here was paid for: the first
five plates made with this tool (PR [#261](https://github.com/arnegiacomo/fugleramme/pull/261))
passed its own checks and the operator's sign-off, and the maintainer sent all five back on
2026-10-11. The recut was approved on the second pass. Read this
before writing a spec, and again before showing anyone a cut.

Upstream's own rules are in its `.claude/skills/plate-review/SKILL.md`,
`docs/adding-artwork.md` and `tools/platereview/README.md`. They win over anything here.

## Every rule and what enforces it

`tools/recut.sh` ends with `tools/qa.py`, which fails the cut on any automated check. The
rest are gates a person passes. Nothing goes upstream with a failing check or a skipped gate.

| Rule | Enforced by |
| --- | --- |
| 1. Keep the whole vignette | `qa.py` *lost*; full-size scan/cut flip in the review page |
| 2. Box holds the whole printed image | `qa.py` *box*; the operator accepts every box on upstream `crop.html` before cutting |
| 3. Never trace from a painted scene | Plate choice (rule 7); `vignette.py` has no trace mode |
| 4. Retone the page, don't threshold | `vignette.py` is the only cutter; `qa.py` *lost* (pale wash) |
| 5. Hold colour to the scan | `tone.py`; `qa.py` *colour* (ratio <= 1.08) |
| 6. Look where damage hides | Agent's full-size close-ups before showing; the operator's review page (`fugart ui`) |
| 7. Choose plates that cut well | Source research report; the operator approves the plate gallery |
| 8. Say what was retouched | `qa.py` writes `disclosure.md`, copied into the PR body |
| 9. Page-tone every pocket | `vignette.py` fills all pockets; `qa.py` *pockets* |
| 10. No floating parts, straight cuts, sharp corners | `qa.py` *floating* and *lost* (a stub at a drop edge fails); review page |
| 11. Box the bird short of long tips | Upstream `bird_box.py`; checked at the operator's final sign-off |
| 12. One to three birds per PR | `fugart batch create` for the batch; PR prep |
| 13. Painted over is lost too | `qa.py` *erased* and `erased.jpg` |
| 14. Weak tone touching ink is drawing | `vignette.py` `reach`; `qa.py` *erased* foxing split |
| 15. Pale washes inside a body are not paper | `protect` by hand; `qa.py` *erased* |
| 16. Protect polygons sit inside an open edge | Spec review; `qa.py` *erased*; review page outline |
| 17. A wider outline brings back foxing | Old against new cut before showing; `qa.py` *lost* specks |
| 18. Check at the size the maintainer sees | `erased.jpg` shipped column; agent's close-ups; review page |
| 19. Circles say where, not everything | Agent's whole-plate pass after each review |

Also not optional:
- Seeds are in **working-crop** px: `plate.py` shrinks a crop to 2400 px on its long side, so
  a seed is `(scan point - box corner) * 2400 / longest side of the box`.
  Draw the seeds on `crop.png` and look before cutting.
- A changed rule goes in this file and, where it can, into `qa.py` with a test.

## What the review said

| Plate | Maintainer's comment | What the first pass did | Fix |
| --- | --- | --- | --- |
| Maned Duck | Rough edges from background removal; use the whole plate and strip only the sky | Traced the pair and the log out of a painted scene | Whole plate, sky retoned to the page |
| Black Swan | Same, rough on neck and head; use the whole plate | Traced the swan out of painted water and reeds | Whole plate, sky retoned to the page |
| Welcome Swallow | Cut and editing good; include more of the original subject | Trimmed the trunk to a compact pair-and-nest | Whole trunk and leaves back |
| Spotted Pardalote | Flowers, leaves and a bit of tail accidentally cut | Removed the overhead spray; pale blossom eaten by the paper threshold | Whole vignette, nothing thresholded |
| Eastern Rosella | Tail feathers and leaves eaten into; stripper too aggressive; oversaturated | Paper threshold lowered to 166/172, which is paler than the leaves and tail tips | Whole vignette retoned, colour held to the scan |

On [#348](https://github.com/arnegiacomo/fugleramme/pull/348) (2026-10) the same fault came
back in a quieter form, past `qa.py`:

| Plate | Maintainer's comment | What the cut did | Fix |
| --- | --- | --- | --- |
| Grey Shrike-thrush | White plumage and perch ends removed by the background remover | Faded the white chin by the bill and the dry branch end to page tone; also whitened the streaked breast of the lower bird | Cutter keeps faint tone joined to ink; `protect` over the throats and breast |
| Golden Whistler | White plumage, flowers and perch ends removed | Faded both chins, the lower wattle blossom and two dry twig ends to page tone | Same cutter change; `protect` over the white chin and the pale bud; two foxing specks dropped |

The cutter never made these transparent. It painted them in page colour and kept them
opaque, so the *lost* check, which only reads transparency, passed them. Rule 13 and the
*erased* check close that gap. The recut was approved and merged on 2026-10-31, and both
merged plates pass *erased*.

His summary on #261: recut all five, less aggressively. And: LLM cuts work best on birds drawn on
white paper, where there is an outline to follow.

## Rules

1. **Keep the whole drawn vignette.** Every bird, its perch, plants and painted background.
   Only the page, caption, signatures, plate numbers and sex marks go. This is upstream's rule
   and the first pass broke it three ways: dropped a painted background, tightened a composition, trimmed
   foliage. A tidier composition is not a reason. If the operator asks for a trim, say that the
   maintainer sent trims back on #261 before doing it.

2. **The crop box holds the whole printed image.** Draw the box on the full scan and look.
   The duck and swan boxes clipped the stump, reeds, bulrush and the water's own soft ends,
   which is half of why those plates then needed tracing.

3. **Never trace a bird out of a painted scene.** There is no drawn edge between plumage and
   painted water, so any outline is rough, and it shows most on a neck or head against open
   ground. Keep the plate and remove only the sky.

4. **Retone the page, do not cut into it.** `plate.py` decides paper by one brightness
   threshold. Pale leaves, blossom, white tail tips and faint stalks are brighter than an aged
   page, so they are "paper" and vanish, and lowering `paper_min`/`paper_clean` for a dark
   gutter makes it worse. `tools/vignette.py` instead estimates the page (or sky) as a smooth
   field, divides it out so it lands on the halo tone, and keeps everything that is left. If
   a spec needs `paper_min` under about 220, or more than a couple of `keep` polygons, stop
   and use `vignette.py`.

5. **Hold colour to the scan.** `finish.py` deepens a faded scan, and that stretches colour
   too: the rosella came out 30% more saturated than its plate. `tools/tone.py` scales colour
   back so the mean saturation matches the balanced scan and keeps the deepened darks. Run it
   on every plate and put the finished cut beside the scan before judging colour.

6. **Look where the damage hides.** Thumbnails and contact sheets hid every one of these
   faults. Before showing a cut, zoom scan against cut at full size on: every tail tip, every
   pale leaf and blossom, every thin stalk and every perch or twig end, the head and neck
   edge, the chin and throat beside the bill, every white or pale breast, and the whole
   outline once round. A fade to page tone is invisible on the paper view: flip to the scan. Check `N px not connected to a seed` in the cut's output and find what it was.

7. **Choose plates that cut well.** A bird on white paper with its plants is a clean cut. A
   full painted scene is a whole-plate job: fine, but budget for it and expect a wide plate.

8. **Say what was retouched.** The PR names any retoned sky, any colour correction and any
   occlusion. An undisclosed edit reads as a defect.

9. **Page-tone every pocket; never cut one open.** Paper enclosed by drawing (between
   leaves, a twig and a body, toes and a perch) stays, toned to the halo. The maintainer did
   this himself on [#322](https://github.com/arnegiacomo/fugleramme/pull/322) ("the pocket is
   now page-toned instead of cut open, so the twig is whole"), flagged pockets left in the old
   paper colour on [#270](https://github.com/arnegiacomo/fugleramme/pull/270) and
   [#286](https://github.com/arnegiacomo/fugleramme/pull/286), and upstream's `plate.py` keeps
   enclosed paper. `vignette.py` fills every pocket at full size; `qa.py` fails any left open.
   An early version left square-cornered open pockets from a quarter-size fill.

10. **No straight cuts, sharp corners or floating parts.** From the maintainer's reviews of
    other contributors' PRs ([#124](https://github.com/arnegiacomo/fugleramme/pull/124) and
    others): no floating leaves, twigs or perches; no gaps in a bird's body; no straight cut
    across a perch or leaf (keep the whole leaf or drop it); no outline at too sharp an angle.

11. **Box the bird short of long tips.** Upstream (`2cf70e5`, `docs/adding-artwork.md`): a long
    bill, tail, legs or outstretched wings make the bird render too small, so the bird box
    leaves out their outer part, but not all of it, or the bird renders oversized.

12. **One to three birds per PR.** The maintainer on issue #236: small PRs review faster and
    need fewer passes. Claim the species in an upstream issue first.

13. **Painted over is lost too.** A cut can erase without cutting. `vignette.py` fades faint
    drawing toward the page colour and keeps it opaque, and the halo is a fixed cream, so
    anything that ends up that colour disappears into it. White plumage, pale blossom and
    dry-brush ends sit within a few levels of the page, so any step that treats "near page
    tone" as page flattens them, whether that is the background estimate or the matte: a chin
    under the bill (the gape lets the page in), a white breast, a dry twig or perch end.
    `qa.py` *erased* compares tone against the scan and fails any drawing the cut kept at
    under half its contrast. A faded spot with no path to ink is reported as *foxing* and
    does not fail. `erased.jpg` shows every spot, foxing included, as scan, cut and final
    at shipped size: look at each one. A dry-brush end can be too faint to count as ink and
    land under *foxing*, and fine bristles at the bill can be too thin to flag at all, so
    the full-size look in rule 6 still stands. Fix each loss with `protect` (rule 16) or a
    larger `reach` (rule 14), then recut. Foxing that should go gets a `drop` polygon,
    which the PR discloses. Never raise the limits to pass.

14. **Weak tone touching ink is drawing; weak tone on its own is foxing.** Feather edges,
    bristles by the bill and the tails of brush strokes are faint but joined to real ink, so
    they stay. Faint marks with no path to ink are foxing or show-through and may go. Judge
    every faint spot by this before keeping or dropping it. `vignette.py` applies it: faint
    tone that a seed reaches through ink, within `reach` px (default 48) of ink, is kept as
    painted. That change is what got #348 approved.

15. **Pale washes inside a body are not paper.** A cream or white breast close to page tone
    gets sampled into the page estimate and white-balanced to a blotchy white (the lower
    Shrike-thrush on #348). Nothing enclosed by the drawing should be sampled as page. Until
    `vignette.py` does that itself, cover every pale body area with `protect`.

16. **Protect polygons sit inside an open edge.** Inside a body, draw `protect` just outside
    the paint. Where a pale area meets open page, keep the polygon a few px inside the drawn
    edge: the halo follows a polygon that reaches into the page, and leaves a straight edge in
    the outline (rule 10).

17. **A wider outline brings back foxing.** Keeping more faint detail widens the outline and
    its halo, and specks that sat outside it now show. After any change that widens the
    outline, compare the new cut against the old one for marks that were not visible before.

18. **Check at the size the maintainer sees.** On #348 full-size crops of scan and cut looked
    fine; the losses showed in the shipped 1200 px image on the app's background. At every
    flagged spot, compare scan, shipped and new cut side by side, on paper and on a
    contrasting background.

19. **A reviewer's circles say where, not everything that is wrong.** Map each circle to its
    cause, then look for that cause across the whole plate. Every circle on #348 had one
    cause, and looking for it found a fault nobody circled (rule 15).

## How to recut with vignette.py

A candidate is a folder holding `crop.png` (from `plate.py crop`), `spec.json` (the approved
scan, the box, `rotate` if any) and `params.json`:

```bash
tools/recut.sh /path/to/fugleramme work/experiments/<topic>/<species>/v001
```

That runs `vignette.py` (writes `cut.png`, `check.jpg`, `debug-mask.png`, `cut-stats.json`),
upstream's `finish.py` (writes `final-finish.png`) and `tone.py` (writes `final.png`,
`tone.json`). Then `fugart replace <species> --candidate <folder> --notes "..."` files it as a
new immutable revision, re-running the crop natively when the box changed, and the bird needs
a fresh review and sign-off.

`params.json`:

- `seeds`: a point inside the drawing, crop px. One more for each detached part.
- `balance`: the scan's paper RGB. Only used outside `zone`.
- `protect`: polygons over pale washes that are subject (a tree trunk, painted water, a white
  chin or breast, blossom, a dry twig end) and would otherwise be read as page. They are never sampled as background and never faded.
  Draw them just outside the paint, or a few px inside where the area meets open page (rule 16).
- `drop`: polygons removed outright: caption, signatures, an ink speck.
- `fade`: `[polygon, px]` as in `plate.py`, where a wash has no edge of its own.
- `zone`: polygons to flatten in, when the page should not be estimated everywhere.
- `reach`: px from ink within which faint tone joined to the drawing is kept (default 48).

Read `debug-mask.png` after every run: white is kept drawing, grey the margin, red is ink
that was not connected to a seed and got dropped.

Keep each candidate's `params.json` with its cut: `fugart replace` files the recipe with the
revision, so the next plate can start from one that worked.
