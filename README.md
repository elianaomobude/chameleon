# CHAMELEON — starter repo

This is the scaffolding for your engine. It handles input loading, output
writing, rendering, and Docker packaging — so the only file you need to
edit is **`engine/resize.py`**.

```
input:  a task JSON (source design + target canvas)
              |
              v
        engine/resize.py   <-- YOU IMPLEMENT THIS
              |
              v
output: <task>_output.json  (schema-valid resized design)
        <task>_output.png   (rendered preview)
```

Both the JSON and the PNG are what get evaluated — see `RULES.md`.

## Repo layout

```
engine/
  resize.py        <- implement your approach here
  main.py           <- orchestrator (input -> resize() -> output). Don't need to edit this.
renderer/           <- the real renderer (transformer-convertor + cr-renderer), already vendored
brand_kit/
  assets/           <- image files referenced by asset_id in the sample designs
  fonts/            <- font files matching the schema's font_family values
input/               <- drop task JSON files here for local testing (3 samples included)
output/              <- results land here
chameleon_design_schema_v0.9.json   <- full field-by-field spec for the design format
Dockerfile
RULES.md
```

## Quick start (no Docker needed for iterating on `resize()`)

```bash
pip install -r requirements.txt
python -m engine.main
```

This processes every `*.json` file in `input/` (the 3 sample designs are
already there — one per difficulty level) and writes an `_output.json` and
`_output.png` for each into `output/`.

The included baseline is a **uniform stretch** — deliberately the wrong
approach (see the main brief: "a naive program stretches the picture; a
good one moves the furniture"). It's there so the pipeline runs on day
one. Run it once, look at `output/*_output.png`, and you'll see exactly
why it's not good enough — that's the point. Replace the body of
`resize()` in `engine/resize.py`, not just its constants.

## What the self-check does (and doesn't do)

`main.py` runs a quick structural check after every task — same element
`id`s as the source, required fields present, nothing off-canvas. **This
is not the official scorer.** It exists to catch obvious mistakes before
you waste a submission on them. See `RULES.md` for what's actually
checked and graded.

A self-check warning does not fail the run — your JSON and PNG are still
written either way. Read the warning; decide whether it's a real problem
or, like the Level 2 sample's intentionally corner-bleeding decorative
circle, an expected one.

## The renderer

`main.py` calls `renderer/converter/convert.py`'s `render_design_to_png()`
(a vendored copy of `transformer-convertor`, wrapping `cr-renderer`) to
produce each task's PNG — already wired in, no setup needed. It reads
fonts/assets from `brand_kit/` and writes a real rendered preview, not a
placeholder. If you ever see plain labeled boxes instead of real text and
images in `output/*.png`, that means the import in `main.py` failed (e.g.
`cr-renderer` didn't install — check `requirements.txt` installed cleanly)
and it silently fell back to the box-and-label placeholder; don't judge
your layout quality by that fallback.

## Docker (how you'll actually be run)

```bash
docker build --platform linux/amd64 -t my-chameleon-engine .
docker run --rm --network none \
  -v $(pwd)/input:/app/input \
  -v $(pwd)/output:/app/output \
  my-chameleon-engine
```

`--network none` matches how stage-one scoring runs: everything your
engine needs must be installed at **build** time (add it to
`requirements.txt`), not fetched at run time. If you're using a locally
hosted model, bake its weights into the image during the build step.

## See also

- `chameleon_design_schema_v0.9.json` — full input/output field spec
- `RULES.md` — what's checked, what's graded, and what disqualifies a submission
- FAQ — for anything not covered above


# CHAMELEON — Resize Engine

Our engine **re-lays-out** a design for a new canvas instead of stretching it. It is deterministic, runs fully offline, uses no AI model, and needs nothing beyond the starter `requirements.txt`.

## Approach (`engine/resize.py`)

1. **Read the design from geometry.** With no labels to go on, it infers each element's role from size, position and type: background, headline, logo, hero image, call to action, fine print. Grouped elements, and text sitting on a shape, move as one unit.
2. **Lay out by visible artwork.** Images often carry transparent padding, so spacing is measured on what is actually visible, keeping gaps even to the eye.
3. **Search and score layouts.** It tries splitting the content into 1–5 columns, fits each with the renderer's real font metrics, and scores the candidates like a designer would: larger is better, the headline stays with its subhead, and columns stay balanced. Tall canvases end up stacked; wide banners end up in columns.
4. **Keep text readable.** No visible text drops below a readable size, and the headline always stays the loudest element.
5. **Hide only what can't fit.** On extreme shapes such as 728×90, low-priority items (fine print, extra badges) are made invisible when they cannot be shown legibly. They keep their ids, colours and proportions, and each is brought back whenever it fits. The headline, CTA, logo and hero image are never hidden.
6. **Validate and fall back.** Every result is checked against the hard rules: all ids present, nothing off-canvas, images in proportion, text and fonts unchanged. A safe layout is returned if any check fails, so the engine never crashes.

## How to run

```bash
pip install -r requirements.txt
python -m engine.main       # input/*.json -> output/*_output.json + .png
python -m engine.validate   # rule check report
```

Docker (PowerShell, all on one line):

```powershell
docker build --platform linux/amd64 -t my-chameleon-engine .
docker run --rm --network none -v "${PWD}/input:/app/input" -v "${PWD}/output:/app/output" my-chameleon-engine
```

## Results and limitations

- **Official samples:** all three pass with 0 hard failures and 0 warnings.
- **Stress test:** 30 resizes across 10 common formats, with 0 hard failures. Text falls below the readability threshold only on the tiniest formats (160×600, 320×50).
- **Not guaranteed:** these are local results and do not guarantee performance on hidden designs.
- **Not yet handled:** text contrast checking, anchoring decorations to the element they sat behind, and preserving left alignment.
- **Speed:** Level 3 takes roughly 15–50 seconds because of the layout search.


