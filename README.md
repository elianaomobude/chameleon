# CHAMELEON — Resize Engine

Our engine **re-lays-out** a design for a new canvas instead of stretching it. It is deterministic, runs fully offline, uses no AI model, and needs nothing beyond the starter `requirements.txt`.

## Approach (`engine/resize.py`)

1. **Read the design from geometry.** With no labels to go on, it infers each element's role from size, position and type: background (an image or a solid colour), headline, logo, hero image, call to action, fine print. Grouped elements, text sitting on a shape, and matching images side by side all move as one unit.
2. **Lay out by visible artwork.** Images often carry transparent padding, so spacing is measured on what is actually visible, keeping gaps even to the eye.
3. **Search and score layouts.** It tries splitting the content into 1–5 columns, fits each with the renderer's real font metrics, and scores the candidates like a designer would: larger is better, the headline stays with its subhead (and sits snugly above it when they touched in the original), and columns stay balanced. Tall canvases end up stacked; wide banners end up in columns. If the original lines its content up on a left (or right) edge, the resized version keeps that shared edge; centred designs stay centred.
4. **Keep text readable.** No visible text drops below a readable size, and the headline always stays the loudest element.
5. **Hide only what can't fit.** On extreme shapes such as 728×90, low-priority items (fine print, extra badges) are made invisible when they cannot be shown legibly. They keep their ids, colours and proportions, and each is brought back whenever it fits. The headline, CTA, logo and hero image are never hidden.
6. **Keep decorations with their content.** A translucent band or glow stays behind the picture or button it originally sat behind, and never sits behind the logo, so the logo always reads cleanly.
7. **Designer rules for banners.** A small badge (such as "NEW FLAVOR") sits directly under the product it describes. On banners, a button stays at about a quarter of the width or less, so it never overpowers the message. On thin strips (5:1 or wider), the logo opens on the left, the message follows, and the product sits right beside the call to action, with wider gaps between pieces. The call to action is the only button-like shape on a strip, so other pill badges step aside, and a rotated promo sticker (such as "SUNSET SALE") steps aside too, so the strip carries one clear message. Every piece on a strip shares one vertical centreline and the row keeps equal safe margins on the left and right: the product fills the strip's height, the button's pill hugs its label, and the headline gets the width it needs to wrap into as few lines as possible. Secondary badges are kept smaller than the call to action, so the button stays the most obvious thing to click.
8. **Validate and fall back.** Every result is checked against the hard rules: all ids present, nothing off-canvas, images in proportion, text and fonts unchanged. A safe layout is returned if any check fails, so the engine never crashes.

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
- **Stress test:** the 3 samples plus 8 extra designs we built to break our own assumptions (left-aligned, solid-colour background, two products, text-heavy, landscape source, rotated stickers, a single line, missing assets), each resized to 8 common formats. 88 resizes in total, with 0 crashes, 0 hard failures and 0 fallbacks. Text falls below the readability threshold only on the tiniest formats (160×600).
- **Not guaranteed:** these are local results and do not guarantee performance on hidden designs.
- **Not yet handled:** text contrast checking.
- **Speed:** most designs take under a second; the most complex sample takes about 8–15 seconds because of the layout search. Text measurements are cached per character, giving identical results to the renderer.