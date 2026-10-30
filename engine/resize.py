
"""
CHAMELEON engine — reflow, not stretch.

Approach (no ML model, fully deterministic, runs offline in milliseconds):

1. CLASSIFY every element from geometry alone (no semantic labels needed):
   - background : a full-bleed image (covers most of the canvas)  -> fills the new canvas
   - decoration : low-opacity / partly off-canvas ungrouped shapes, or images whose
                  asset file is missing -> re-anchored to the nearest corner, scaled
   - content    : everything else. Elements sharing a group_id move as ONE rigid
                  unit (a button = its shape + its label), so groups never break.

2. RANK content blocks by importance (headline = biggest type, hero image = biggest
   image, CTA = short text on a filled shape, legal = smallest type, ...).

3. SEARCH LAYOUTS. Content blocks are kept in their original reading order and
   split into 1..N side-by-side columns (contiguous partitions). For each candidate
   we binary-search the largest uniform scale k at which every column fits:
   images keep their aspect ratio, text is re-wrapped with real font metrics at
   the new column width. The layout with the best scale wins (fewer columns win ties).
   Tall canvases naturally choose 1 column, wide banners choose 3-4.

4. GRACEFUL DROP. If even the best layout would push text below a readable size
   (extreme ratios like 728x90), the lowest-priority blocks (legal, watermark,
   badges, long body copy) are collapsed one at a time: they stay in the output
   with their ids (as the rules require) but become invisible (opacity 0, 1px,
   tucked in a corner). The headline, CTA, logo and hero are never dropped.

5. Everything is clamped inside the canvas, ids/fonts/content are untouched.
"""

from __future__ import annotations

import copy
import itertools
import math
import os
from pathlib import Path
from typing import Any

LINE_HEIGHT = 1.2
_MIN_PX = [10.0]
_IMG_BOOST = [1.0]  # pictures may outgrow the type scale on very wide banners

# --------------------------------------------------------------------------
# Text measurement (real brand-font metrics via Pillow, with a safe fallback)
# --------------------------------------------------------------------------
_FONT_DIRS = [Path("brand_kit/fonts"), Path(__file__).resolve().parent.parent / "brand_kit" / "fonts"]
_ASSET_DIRS = [Path("brand_kit/assets"), Path(__file__).resolve().parent.parent / "brand_kit" / "assets"]
_font_cache: dict = {}


def _font_file(family: str, bold: bool, italic: bool) -> Path | None:
    stem = family.replace(" ", "")
    suffix = ("Bold" if bold else "") + ("Italic" if italic else "")
    suffix = suffix or "Regular"
    for d in _FONT_DIRS:
        for cand in (f"{stem}-{suffix}.ttf", f"{stem}-Bold.ttf" if bold else f"{stem}-Regular.ttf"):
            p = d / cand
            if p.exists():
                return p
    return None


def _get_font(el: dict, size: float):
    bold = str(el.get("font_weight", "")).lower() in ("bold", "700", "800", "900", "semibold", "600")
    italic = el.get("font_style") == "italic" or el.get("italic") is True
    key = (el.get("font_family", ""), bold, italic, round(size * 4) / 4)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    try:
        from PIL import ImageFont

        f = _font_file(el.get("font_family", ""), bold, italic)
        if f is not None:
            font = ImageFont.truetype(str(f), max(1, int(round(size))))
    except Exception:
        font = None
    _font_cache[key] = font
    return font


_skia = None


def _skia_width(el, size, s):
    global _skia
    if _skia is None:
        try:
            from renderer.converter.brand_fonts import load_brand_font_manager
            from renderer.converter.wrap import char_widths
            fdir = next((d for d in _FONT_DIRS if d.exists()), None)
            _skia = (load_brand_font_manager(str(fdir)), char_widths) if fdir else False
        except Exception:
            _skia = False
    if not _skia:
        return None
    fm, cw = _skia
    w = str(el.get("font_weight", "regular")).lower()
    bold = w in ("bold", "700", "800", "900", "semibold", "600")
    italic = el.get("font_style") == "italic" or el.get("italic") is True
    style = ("bold" if bold else "") + ("italic" if italic else "") or "regular"
    try:
        return float(sum(cw(fm, el.get("font_family", ""), "bold" if bold else "regular", style, size, s)))
    except Exception:
        return None


_w_cache: dict = {}


def _text_width(el: dict, size: float, s: str) -> float:
    key = (el.get("font_family"), el.get("font_weight"), el.get("font_style"), round(size, 2), s)
    if key in _w_cache:
        return _w_cache[key]
    v = _skia_width(el, size, s)
    if v is None:
        v = _pil_width(el, size, s)
    _w_cache[key] = v
    return v


def _pil_width(el: dict, size: float, s: str) -> float:
    font = _get_font(el, size)
    if font is not None:
        try:
            return float(font.getlength(s))
        except Exception:
            pass
    return len(s) * size * 0.55


def _wrap_lines(el: dict, size: float, max_w: float) -> tuple[int, float]:
    """Greedy word wrap (mirrors the renderer). Returns (n_lines, widest_line)."""
    content = str(el.get("content", ""))
    lines, widest = 0, 0.0
    for para in content.split("\n"):
        words = para.split(" ")
        cur = ""
        for w in words:
            trial = w if not cur else cur + " " + w
            if cur and _text_width(el, size, trial) > max_w:
                lines += 1
                widest = max(widest, _text_width(el, size, cur))
                cur = w
            else:
                cur = trial
        lines += 1
        widest = max(widest, _text_width(el, size, cur))
    return lines, widest


def _text_height(el: dict, size: float, max_w: float) -> tuple[float, float]:
    n, widest = _wrap_lines(el, size, max_w)
    lh = float(el.get("line_height", LINE_HEIGHT))
    return n * size * lh, widest


def _asset_exists(asset_id: str) -> bool:
    for d in _ASSET_DIRS:
        if (d / asset_id).exists():
            return True
    # if we can't see any asset dir at all, don't assume missing
    return not any(d.exists() for d in _ASSET_DIRS)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def _area(e):
    return max(0.0, e["width"]) * max(0.0, e["height"])


def _bbox(els):
    x0 = min(e["x"] for e in els)
    y0 = min(e["y"] for e in els)
    x1 = max(e["x"] + e["width"] for e in els)
    y1 = max(e["y"] + e["height"] for e in els)
    return x0, y0, x1 - x0, y1 - y0


class Block:
    """A unit that moves rigidly: a single element or a whole group."""

    def __init__(self, els: list[dict], src_canvas):
        self.els = els
        self.x, self.y, self.w, self.h = _bbox(els)
        self.texts = [e for e in els if e["type"] == "text"]
        self.images = [e for e in els if e["type"] == "image"]
        self.shapes = [e for e in els if e["type"] == "shape"]
        self.is_group = len(els) > 1
        # a lone text block reflows (its width follows the column);
        # images / groups scale rigidly.
        self.flow_text = (not self.is_group) and len(self.texts) == 1
        self.priority = 5.0
        self.tf = 1.0  # extra type scale for reflowing text (extreme ratios)
        self.dropped = False
        self.cw, self.ch = src_canvas

    @property
    def max_font(self):
        return max([t["font_size"] for t in self.texts] or [0])


def _classify(source: dict):
    cw, ch = source["canvas"]["width"], source["canvas"]["height"]
    canvas_area = cw * ch
    bgs, decos, content = [], [], []
    groups: dict[str, list] = {}

    for e in source["elements"]:
        a = _area(e)
        off = e["x"] < -1 or e["y"] < -1 or e["x"] + e["width"] > cw + 1 or e["y"] + e["height"] > ch + 1
        if e["type"] == "image" and (a >= 0.85 * canvas_area or (e.get("fit") == "cover" and a >= 0.5 * canvas_area)):
            bgs.append(e)
        elif e["type"] == "shape" and not e.get("group_id") and (
            e.get("opacity", 1.0) < 0.5 or off or a >= 0.6 * canvas_area
        ):
            decos.append(e)
        elif e["type"] == "image" and not _asset_exists(e.get("asset_id", "")):
            decos.append(e)
        elif e.get("group_id"):
            groups.setdefault(e["group_id"], []).append(e)
        else:
            content.append([e])

    # implicit groups: a text label sitting on a filled shape (pill / button / tag) without
    # a group_id is treated as one unit, exactly like an explicit group.
    shapes = [c[0] for c in content if c[0]["type"] == "shape"]
    texts = [c[0] for c in content if c[0]["type"] == "text"]
    merged_ids = set()
    for t in texts:
        tcx, tcy = t["x"] + t["width"] / 2, t["y"] + t["height"] / 2
        host = None
        for sh in shapes:
            if sh["id"] in merged_ids and host is None:
                pass
            if (sh["x"] <= tcx <= sh["x"] + sh["width"] and sh["y"] <= tcy <= sh["y"] + sh["height"]
                    and t["width"] <= sh["width"] * 1.15 and t["height"] <= sh["height"] * 1.15
                    and _area(sh) < 0.25 * canvas_area):
                if host is None or _area(sh) < _area(host):
                    host = sh
        if host is not None:
            key = "__implicit_" + host["id"]
            groups.setdefault(key, [host]).append(t)
            merged_ids.update({host["id"], t["id"]})
    content = [c for c in content if c[0]["id"] not in merged_ids]

    blocks = [Block(g, (cw, ch)) for g in content] + [Block(g, (cw, ch)) for g in groups.values()]
    _rank(blocks, cw, ch)
    blocks.sort(key=lambda b: (round((b.y + b.h / 2) / (ch * 0.04)), b.x))  # reading order
    return bgs, decos, blocks


def _rank(blocks: list[Block], cw, ch):
    fonts = [t["font_size"] for b in blocks for t in b.texts]
    max_font = max(fonts or [1])
    min_font = min(fonts or [1])
    imgs = [b for b in blocks if b.images and not b.texts]
    biggest_img = max(imgs, key=lambda b: b.w * b.h, default=None)

    cands = [b for b in blocks if b.is_group and b.texts and b.shapes
             and not any(abs(e.get("rotation", 0) or 0) > 1 for e in b.els)]
    cta = max(cands, key=lambda b: (b.h * b.w) * (0.5 + (b.y + b.h / 2) / ch), default=None)
    for b in blocks:
        p = 4.0
        if b.texts and not b.is_group:
            t = b.texts[0]
            n = len(str(t.get("content", "")))
            if t["font_size"] >= max_font * 0.95:
                p = 10.0  # headline
            elif t["font_size"] <= min_font * 1.05 and len(fonts) > 2 and t["font_size"] < max_font * 0.4:
                p = 1.5  # legal / fine print
            else:
                p = 6.0 if n < 120 else 4.5  # subhead vs long body
        elif b.is_group and b.texts and b.shapes:
            label = " ".join(str(t.get("content", "")) for t in b.texts)
            rel_y = (b.y + b.h / 2) / ch
            rot = any(abs(e.get("rotation", 0) or 0) > 1 for e in b.els)
            if rot:
                p = 3.0  # rotated ribbon / sticker
            elif b is cta:
                p = 9.0  # call to action button
            else:
                p = 3.5 if len(label) < 30 else 3.0  # badge / pill
        elif b.images and not b.texts:
            aid = " ".join(str(i.get("asset_id", "")) for i in b.images).lower()
            if "logo" in aid or (b.y + b.h / 2) / ch < 0.15 and b.w / max(b.h, 1) > 2:
                p = 9.5  # logo
            elif b is biggest_img:
                p = 8.0  # hero / product
            else:
                p = 3.0
        b.priority = p


# --------------------------------------------------------------------------
# Layout search
# --------------------------------------------------------------------------
def _block_size(b: Block, k: float, col_w: float, max_h: float = 1e9):
    """Size of block at scale k inside a column of width col_w (or None if it can't fit)."""
    if b.flow_text:
        t = b.texts[0]
        fs = t["font_size"] * k * b.tf
        natural_w = _text_width(t, fs, str(t.get("content", "")))
        pad = fs * 0.5 + 2  # safety so the renderer never wraps to an extra line
        w = min(col_w - pad, max(natural_w, fs))
        if w <= 0:
            return None
        h, widest = _text_height(t, fs, w)
        # a single word wider than the column is a failure (would be cut off)
        if widest + pad > col_w + 0.5:
            return None
        return widest + pad, h + fs * 0.25, k
    if b.images and not b.texts and not b.shapes:
        # pictures shrink independently to fit the band (keeps proportion)
        s_ = min(k * _IMG_BOOST[0], max_h / max(b.h, 1))
        return b.w * s_, b.h * s_, s_
    # rigid groups (buttons, pills) may also grow past the type scale on banners
    # ...but only as far as needed to make their labels readable (hierarchy stays intact)
    need = (_MIN_PX[0] * 1.15) / max(min([t["font_size"] for t in b.texts] or [1e9]), 1e-6)
    k_g = max(k, min(k * 2.5, max_h / max(b.h, 1), need))
    w, h = b.w * k_g, b.h * k_g
    if w > col_w + 0.5:
        k_g = k
        w, h = b.w * k, b.h * k
        if w > col_w + 0.5:
            return None
    k = k_g
    # group text must still fit inside its own (scaled) box
    for t in b.texts:
        fs = t["font_size"] * k
        n, widest = _wrap_lines(t, fs, t["width"] * k)
        if widest > t["width"] * k + 1:
            return None
    return w, h, k


def _fits(cols, k, W, H, gap, margin):
    """Check a column partition at scale k. Returns placement or None."""
    avail_w = W - 2 * margin - gap * (len(cols) - 1)
    avail_h = H - 2 * margin
    if avail_w <= 0 or avail_h <= 0:
        return None
    # fixed width per column = widest rigid block; flexible text gets remaining share
    fixed = []
    for col in cols:
        fw = max([(b.w * min(k * _IMG_BOOST[0], avail_h / max(b.h, 1)) if (b.images and not b.texts and not b.shapes)
                   else b.w * max(k, min(k * 2.5, avail_h / max(b.h, 1),
                                         (_MIN_PX[0] * 1.15) / max(min([t["font_size"] for t in b.texts] or [1e9]), 1e-6))))
                  for b in col if not b.flow_text] or [0])
        fixed.append(fw)
    flex_cols = [i for i, col in enumerate(cols) if any(b.flow_text for b in col)]
    rest = avail_w - sum(fixed[i] for i in range(len(cols)) if i not in flex_cols)
    widths = list(fixed)
    if flex_cols:
        # give text columns a share proportional to their text mass
        mass = []
        for i in flex_cols:
            m = sum(len(str(b.texts[0].get("content", ""))) * b.texts[0]["font_size"] for b in cols[i] if b.flow_text)
            mass.append(max(m, 1) ** 0.5)
        tot = sum(mass)
        for i, m in zip(flex_cols, mass):
            widths[i] = max(fixed[i], rest * m / tot)
    if sum(widths) > avail_w + 1:
        return None

    placed_cols = []
    for col, cw_ in zip(cols, widths):
        sizes = []
        for b in col:
            s = _block_size(b, k, cw_, avail_h)
            if s is None:
                return None
            sizes.append(s)
        total_h = sum(s[1] for s in sizes) + gap * 0.6 * (len(col) - 1)
        if total_h > avail_h + 0.5:
            return None
        placed_cols.append((cw_, sizes, total_h))
    return widths, placed_cols


def _partitions(n, m):
    for cuts in itertools.combinations(range(1, n), m - 1):
        bounds = (0,) + cuts + (n,)
        yield [(bounds[i], bounds[i + 1]) for i in range(m)]


def _best_layout(blocks, W, H, src_scale_cap):
    n = len(blocks)
    if n == 0:
        return None
    margin = max(6.0, min(W, H) * 0.07, W * 0.015)
    gap = max(4.0, min(W, H) * 0.04)
    best = None
    max_cols = min(n, 5)
    # supporting text: a text block directly under another text block in the source
    # (headline -> subhead) should stay in the same column.
    src_h = blocks[0].ch
    keep_pairs = [
        i for i in range(n - 1)
        if blocks[i].texts and blocks[i + 1].flow_text
        and 0 <= blocks[i + 1].y - (blocks[i].y + blocks[i].h) < 0.08 * src_h
        and abs((blocks[i].x + blocks[i].w / 2) - (blocks[i + 1].x + blocks[i + 1].w / 2)) < 0.15 * blocks[0].cw
    ]
    # strongly wide canvases want more columns; tall canvases want 1
    ar = W / H
    for m in range(1, max_cols + 1):
        if ar < 0.9 and m > 2:
            break
        parts = list(_partitions(n, m))
        if len(parts) > 400:
            parts = parts[:: max(1, len(parts) // 400)]
        for part in parts:
            cols = [blocks[a:b] for a, b in part]
            lo, hi = 0.0, src_scale_cap
            if _fits(cols, 0.02, W, H, gap, margin) is None:
                continue
            for _ in range(22):
                mid = (lo + hi) / 2
                if _fits(cols, mid, W, H, gap, margin) is not None:
                    lo = mid
                else:
                    hi = mid
            k = lo
            score = _score_candidate(cols, part, k, m, W, H, gap, margin, keep_pairs)
            if best is None or score > best[0]:
                best = (score, k, cols, margin, gap)
    if best is None:
        return None
    _, k, cols, margin, gap = best
    return k, cols, _fits(cols, k * 0.999, W, H, gap, margin), margin, gap


def _score_candidate(cols, part, k, m, W, H, gap, margin, keep_pairs):
    """Judge a candidate like a designer would, not just by size."""
    score = k * (1 - 0.02 * (m - 1))  # bigger is better; fewer columns break ties
    cuts = {a for a, _ in part[1:]}
    for i in keep_pairs:  # headline + its supporting line split apart
        if (i + 1) in cuts:
            score *= 0.85
    if m > 1:  # balanced columns read as intentional
        pl = _fits(cols, k * 0.999, W, H, gap, margin)
        if pl is not None:
            hs = [t for _, _, t in pl[1]]
            score *= 0.92 + 0.08 * (min(hs) / max(max(hs), 1e-6))
    return score


def _min_readable(W, H):
    return max(10.0, min(W, H) * 0.018)


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------
def _place(b: Block, x, y, w, h, k, out: dict):
    if b.flow_text:
        t = out[b.texts[0]["id"]]
        t["x"], t["y"], t["width"], t["height"] = x, y, w, h
        t["font_size"] = max(1.0, round(t["font_size"] * k * b.tf, 2))
        return
    for e in b.els:
        o = out[e["id"]]
        o["x"] = x + (e["x"] - b.x) * k
        o["y"] = y + (e["y"] - b.y) * k
        o["width"] = e["width"] * k
        o["height"] = e["height"] * k
        if e["type"] == "text":
            o["font_size"] = max(1.0, round(e["font_size"] * k, 2))
        if "corner_radius" in e and e["corner_radius"]:
            o["corner_radius"] = e["corner_radius"] * k


def _hide(e: dict, W, H):
    e["x"], e["y"], e["width"], e["height"] = 0, 0, 1, 1
    e["opacity"] = 0.0
    if e["type"] == "text":
        e["font_size"] = 1
        e["color"] = "rgba(0,0,0,0)"
        e.pop("style_runs", None)
    if e["type"] == "shape":
        e["fill_color"] = "rgba(0,0,0,0)"
    if "rotation" in e:
        e["rotation"] = 0


def _hide_block(els: list[dict], out: dict, W, H):
    """Gracefully drop a unit: shrink it uniformly (proportions, group layout, colours and
    text fit all preserved) and make it fully transparent. Only opacity changes."""
    x0, y0, bw, bh = _bbox(els)
    fonts = [e["font_size"] for e in els if e["type"] == "text"]
    s_ = max(4.0 / max(bw, bh, 1e-6), 1.05 / min(fonts) if fonts else 0.0)
    s_ = min(s_, 1.0, (W - 8) / max(bw, 1e-6), (H - 8) / max(bh, 1e-6))
    for e in els:
        o = out[e["id"]]
        o["x"] = 4 + (e["x"] - x0) * s_
        o["y"] = 4 + (e["y"] - y0) * s_
        o["width"] = e["width"] * s_
        o["height"] = e["height"] * s_
        if e["type"] == "text":
            o["font_size"] = e["font_size"] * s_
        if e.get("corner_radius"):
            o["corner_radius"] = e["corner_radius"] * s_
        o["opacity"] = 0.0
    # tuck the dropped unit beneath every other layer (keeps its internal order), so it is
    # covered by the background even where a renderer ignores opacity on text
    floor = min(int(v.get("z_index", 0) or 0) for v in out.values())
    top = max(int(e.get("z_index", 0) or 0) for e in els)
    for e in els:
        out[e["id"]]["z_index"] = int(e.get("z_index", 0) or 0) - top + floor - 1


def _clamp(e: dict, W, H):
    e["width"] = min(max(1.0, e["width"]), W)
    e["height"] = min(max(1.0, e["height"]), H)
    e["x"] = min(max(0.0, e["x"]), W - e["width"])
    e["y"] = min(max(0.0, e["y"]), H - e["height"])



def _layout_decoration(e: dict, o: dict, sw, sh, W, H, k):
    """Anchor decorations to the corner they were nearest to, scaled gently."""
    s = min(sw, sh) if k is None else max(min(sw, sh), min(k, max(sw, sh)))
    w, h = e["width"] * s, e["height"] * s
    if e["type"] == "image" or e.get("shape_type") == "ellipse":
        # keep proportion; shrink to fit the canvas
        f = min(1.0, W / max(w, 1), H / max(h, 1))
        w, h = w * f, h * f
    cx = (e["x"] + e["width"] / 2) / sw  # center in "source" coords scaled
    cy = (e["y"] + e["height"] / 2) / sh
    src_w, src_h = sw, sh  # placeholders
    o["width"], o["height"] = w, h
    return cx, cy


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    """Public entry point: smart reflow, with a never-crash safety net."""
    try:
        result = _reflow(source, target_canvas)
    except Exception:
        return _safe_fallback(source, target_canvas)
    try:  # self-validate: any hard-rule break -> safe, rule-abiding fallback
        from engine.validate import hard_failures
        if hard_failures(source, result):
            return _safe_fallback(source, target_canvas)
    except ImportError:
        pass
    return result


def _safe_fallback(source, target_canvas):
    """Uniform contain-fit + centering: never distorts, never leaves the canvas."""
    W, H = float(target_canvas["width"]), float(target_canvas["height"])
    cw, ch = float(source["canvas"]["width"]), float(source["canvas"]["height"])
    s = min(W / cw, H / ch)
    ox, oy = (W - cw * s) / 2, (H - ch * s) / 2
    els = []
    for e in source["elements"]:
        o = copy.deepcopy(e)
        o["x"], o["y"] = ox + e["x"] * s, oy + e["y"] * s
        o["width"], o["height"] = e["width"] * s, e["height"] * s
        if o["type"] == "text":
            o["font_size"] = max(1.0, e["font_size"] * s)
        if o["type"] == "image" and e.get("fit") == "cover" and _area(e) >= 0.85 * cw * ch:
            o.update({"x": 0, "y": 0, "width": W, "height": H})
        _clamp(o, W, H)
        els.append(o)
    return {"canvas": {"width": target_canvas["width"], "height": target_canvas["height"]}, "elements": els}


def _reflow(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    W, H = float(target_canvas["width"]), float(target_canvas["height"])
    cw, ch = float(source["canvas"]["width"]), float(source["canvas"]["height"])
    sx, sy = W / cw, H / ch

    out = {e["id"]: copy.deepcopy(e) for e in source["elements"]}
    bgs, decos, blocks = _classify(source)

    # 1) background: full bleed, cover-crop
    for e in bgs:
        o = out[e["id"]]
        o.update({"x": 0, "y": 0, "width": W, "height": H})
        o["fit"] = "cover"

    # 2) content: search layouts, dropping lowest-priority blocks if text gets unreadable
    # never blow elements up beyond the overall change in canvas area
    cap = min(3.0, math.sqrt((W * H) / (cw * ch)) * 1.3)
    min_px = _min_readable(W, H)
    _IMG_BOOST[0] = 2.0 if W / H >= 3 else 1.0
    _MIN_PX[0] = min_px
    active = list(blocks)

    def evaluate(act, tfac):
        for b in act:
            b.tf = tfac if b.flow_text else 1.0
        res = _best_layout(act, W, H, cap)
        if res is None:
            return None, False, 0.0
        k_ = res[0]
        scale = {}
        if res[2] is not None:
            for col, (_, sizes, _) in zip(res[1], res[2][1]):
                for b, sz in zip(col, sizes):
                    scale[id(b)] = sz[2] * (b.tf if b.flow_text else 1.0)
        fonts = [t["font_size"] * scale.get(id(b), k_ * b.tf) for b in act for t in b.texts]
        head = [t["font_size"] * scale.get(id(b), k_ * b.tf) for b in act for t in b.texts if b.priority >= 10]
        ok_ = (not fonts or min(fonts) >= min_px)
        if head and fonts:
            ok_ = ok_ and max(head) >= 0.9 * max(fonts)  # headline must stay the loudest text
        return res, ok_, (k_ * (0.85 + 0.15 * tfac) if ok_ else min(fonts or [0]))

    result = None
    while True:
        best = None
        for tfac in (1.0, 0.85, 0.7, 0.55, 0.45, 0.35):
            res, ok, score = evaluate(active, tfac)
            if res is None:
                continue
            # an acceptable layout always beats an unacceptable one; among acceptable ones
            # the biggest wins; if none is acceptable, the most legible one wins.
            if best is None or (ok, score) > (best[1], best[2]):
                best = (res, ok, score, tfac)
        if best is not None:
            for b in active:
                b.tf = best[3] if b.flow_text else 1.0
            result = best[0]
        ok = best is not None and best[1]
        droppable = [b for b in active if b.priority < 8 and len(active) > 2]
        if ok or not droppable:
            break
        victim = min(droppable, key=lambda b: (b.priority, -b.y))
        victim.dropped = True
        active.remove(victim)

    # fill: if the chosen layout leaves the canvas mostly empty, let content grow
    if result is not None and _IMG_BOOST[0] == 1.0:
        k0, cols0, pl0 = result[0], result[1], result[2]
        if pl0 is not None:
            content_h = max(t for _, _, t in pl0[1])
            fill = content_h / max(H - 2 * result[3], 1)
            if fill < 0.6:
                bigger = _best_layout(active, W, H, min(3.0, cap * min(1.6, 0.7 / max(fill, 0.1))))
                if bigger is not None and bigger[0] > k0:
                    result = bigger

    for b in blocks:
        if b.dropped:
            _hide_block(b.els, out, W, H)

    k = None
    if result is not None:
        k, cols, placement, margin, gap = result
        if placement is None:
            placement = _fits(cols, k * 0.99, W, H, gap, margin)
        widths, placed_cols = placement
        # compact each column to what its content actually uses (no dead gaps)
        widths = [min(cw_, max(s[0] for s in sizes) + 4) for cw_, (_, sizes, _) in zip(widths, placed_cols)]
        placed_cols = [(w_, s, t) for w_, (_, s, t) in zip(widths, placed_cols)]
        total_w = sum(widths) + gap * (len(cols) - 1)
        # distribute leftover horizontal space evenly between/around columns
        free_w = W - 2 * margin - total_w
        extra = free_w / (len(cols) + 1) if len(cols) > 1 else free_w / 2
        x = margin + extra
        for col, (col_w, sizes, total_h) in zip(cols, placed_cols):
            # vertical: spread blocks across available height (designed breathing room)
            avail_h = H - 2 * margin
            free_h = avail_h - total_h
            inner = gap * 0.6
            if len(col) > 1 and free_h > 0:
                inner += min(free_h / (len(col) + 1), gap * 2.5)
            used = sum(s[1] for s in sizes) + inner * (len(col) - 1)
            y = margin + (avail_h - used) / 2
            for b, (bw, bh, bs) in zip(col, sizes):
                bx = x + (col_w - bw) / 2  # center in column
                if b.flow_text:
                    # keep the full column width so text_align (center/left) behaves
                    bx_full = x if len(cols) == 1 or b.texts[0].get("text_align", "left") != "left" else x
                    _place(b, bx_full, y, col_w, bh, k, out)
                    if b.texts[0].get("text_align", "left") == "left" and len(cols) == 1:
                        out[b.texts[0]["id"]]["x"] = x
                else:
                    _place(b, bx, y, bw, bh, bs, out)
                y += bh + inner
            x += col_w + gap + extra

    # 3) decorations: anchor to nearest corner, keep proportion, stay on canvas
    for e in decos:
        o = out[e["id"]]
        s = min(sx, sy) if k is None else max(min(sx, sy), min(k, 1.5 * min(sx, sy)))
        w, h = e["width"] * s, e["height"] * s
        f = min(1.0, W / max(w, 1), H / max(h, 1))
        w, h = w * f, h * f
        ccx = (e["x"] + e["width"] / 2) / cw
        ccy = (e["y"] + e["height"] / 2) / ch
        o["width"], o["height"] = w, h
        o["x"] = ccx * W - w / 2
        o["y"] = ccy * H - h / 2
        if e["type"] == "image" and not _asset_exists(e.get("asset_id", "")):
            # missing asset: keep it out of the way rather than show a placeholder box
            _hide_block([e], out, W, H)

    for o in out.values():
        _clamp(o, W, H)

    elements = [out[e["id"]] for e in source["elements"]]
    return {"canvas": {"width": target_canvas["width"], "height": target_canvas["height"]}, "elements": elements}

