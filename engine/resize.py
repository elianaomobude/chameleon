"""
CHAMELEON engine resize module.
"""

from __future__ import annotations

import copy
import itertools
import math
import os
from pathlib import Path
from typing import Any

LINE_HEIGHT = 1.2
# thin strips stay clean: a rotated promo sticker steps aside (set True to tuck it in a corner)
STICKER_ON_STRIPS = False
_MIN_PX = [10.0]
_CANVAS = [1e9, 1e9]
_RESERVE = [0.0, 0.0]  # canvas width kept free at the left / right edge (corner sticker)
_IMG_BOOST = [1.0]  # pictures may outgrow the type scale on very wide banners


# Text measurement (real brand-font metrics via Pillow, with a safe fallback)

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


_char_cache: dict = {}


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
        # the renderer measures a string as the sum of per-character advances, so caching each
        # character's advance per font and size gives identical results, far faster
        fam = el.get("font_family", "")
        wt = "bold" if bold else "regular"
        table = _char_cache.setdefault((fam, wt, style, size), {})
        missing = "".join(sorted(set(s) - table.keys()))
        if missing:
            for ch_, w_ in zip(missing, cw(fm, fam, wt, style, size, missing)):
                table[ch_] = float(w_)
        return float(sum(table[c] for c in s))
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


_vis_cache: dict = {}


def _visible_frac(e: dict):
    """Where the visible (non-transparent) artwork sits inside an image element's box,
    as fractions (x0, y0, x1, y1). Many brand assets carry wide transparent padding;
    laying out by the box instead of the art makes spacing look random."""
    key = (e.get("asset_id"), round(e["width"], 1), round(e["height"], 1), e.get("fit", "contain"))
    if key in _vis_cache:
        return _vis_cache[key]
    frac = (0.0, 0.0, 1.0, 1.0)
    try:
        from PIL import Image
        path = next((d / e["asset_id"] for d in _ASSET_DIRS if (d / e["asset_id"]).exists()), None)
        if path is not None and e.get("fit", "contain") != "cover":
            im = Image.open(path)
            aw, ah = im.size
            bb = im.getchannel("A").getbbox() if "A" in im.getbands() else im.getbbox()
            if bb:
                bw, bh = float(e["width"]), float(e["height"])
                c = min(bw / aw, bh / ah)  # contain fit
                ox, oy = (bw - aw * c) / 2, (bh - ah * c) / 2
                frac = ((ox + bb[0] * c) / bw, (oy + bb[1] * c) / bh,
                        (ox + bb[2] * c) / bw, (oy + bb[3] * c) / bh)
    except Exception:
        pass
    _vis_cache[key] = frac
    return frac


def _asset_exists(asset_id: str) -> bool:
    for d in _ASSET_DIRS:
        if (d / asset_id).exists():
            return True
    # if we can't see any asset dir at all, don't assume missing
    return not any(d.exists() for d in _ASSET_DIRS)


# Classification

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
        self.attach = False
        self.forced = False   # dropped by a layout rule, never restored
        self.sticker = None   # promo sticker riding on this product image
        self.st_scale = 0.0
        self.vis = None
        if len(els) == 1 and els[0]["type"] == "image":
            self.vis = _visible_frac(els[0])
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
        elif e["type"] == "shape" and not e.get("group_id") and a >= 0.85 * canvas_area and not e.get("rotation"):
            bgs.append(e)  # solid-colour background or full-canvas tint: refill the new canvas
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

    # matching images side by side (a product pair, a row of icons) keep their arrangement:
    # similar size, same horizontal band, no group of their own -> one rigid unit
    imgs = [c[0] for c in content if c[0]["type"] == "image" and _area(c[0]) > 0]
    used = set()
    for a_ in imgs:
        if a_["id"] in used:
            continue
        row = [a_]
        for b_ in imgs:
            if b_ is a_ or b_["id"] in used:
                continue
            ca, cb = a_["y"] + a_["height"] / 2, b_["y"] + b_["height"] / 2
            if (abs(ca - cb) < 0.08 * ch and 0.8 <= b_["width"] / max(a_["width"], 1e-6) <= 1.25
                    and 0.8 <= b_["height"] / max(a_["height"], 1e-6) <= 1.25
                    and all(b_["x"] >= r["x"] + r["width"] or b_["x"] + b_["width"] <= r["x"] for r in row)):
                row.append(b_)
        if 2 <= len(row) <= 4:
            used.update(r["id"] for r in row)
            groups["__row_" + a_["id"]] = row
    content = [c for c in content if c[0]["id"] not in used]

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
    # small badges / pills ("NEW FLAVOR") describe the product: keep them next to the hero image
    hero = next((b for b in blocks if b.images and not b.texts and b.priority == 8.0), None)
    if hero is not None:
        pills = [b for b in blocks if b.is_group and b.texts and b.shapes and b.priority == 3.5
                 and not any(abs(e.get("rotation", 0) or 0) > 1 for e in b.els)]
        if pills and len(pills) <= 2:
            rest = [b for b in blocks if b not in pills]
            i = rest.index(hero)
            blocks = rest[:i + 1] + pills + rest[i + 1:]
            for p_ in pills:
                p_.attach = True  # layout search strongly prefers keeping it in the hero's column
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
def _sticker_box(st, scale):
    """Axis-aligned footprint of a (possibly rotated) sticker group at a given scale."""
    a = math.radians(max((abs(e.get("rotation", 0) or 0) for e in st.els), default=0))
    w, h = st.w * scale, st.h * scale
    return w * abs(math.cos(a)) + h * abs(math.sin(a)), w * abs(math.sin(a)) + h * abs(math.cos(a))


def _group_scale(b: Block, k: float, max_h: float) -> float:
    """Scale for a rigid group (button, badge). It may grow past the type scale until its
    label is readable; on banners it is also kept to about a quarter of the canvas width,
    but never so small that its label drops below the readable size."""
    min_font = max(min([t["font_size"] for t in b.texts] or [1e9]), 1e-6)
    banner = _CANVAS[0] / max(_CANVAS[1], 1) >= 3
    need = (_MIN_PX[0] * (1.0 if banner else 1.15)) / min_font
    k_g = max(k, min(k * 2.5, max_h / max(b.h, 1), need))
    if banner:
        floor = _MIN_PX[0] / min_font
        k_g = min(k_g, max(0.25 * _CANVAS[0] / max(b.w, 1), floor))
    if b.priority < 9:  # secondary badges stay quieter than the call to action
        k_g = max(k_g * 0.75, min(k_g, _MIN_PX[0] / min_font))
    return k_g


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
        v = b.vis or (0.0, 0.0, 1.0, 1.0)
        vw, vh = max(v[2] - v[0], 0.05), max(v[3] - v[1], 0.05)
        s_ = min(k * _IMG_BOOST[0], max_h / max(b.h * vh, 1),
                 _CANVAS[1] / max(b.h, 1), _CANVAS[0] / max(b.w, 1))
        bw_ = b.w * vw * s_
        if b.sticker is not None:  # the slot is wide enough for the sticker riding on the product
            bw_ = max(bw_, _sticker_box(b.sticker, b.st_scale)[0])
        return bw_, b.h * vh * s_, s_
    # rigid groups (buttons, pills) may also grow past the type scale on banners
    # ...but only as far as needed to make their labels readable (hierarchy stays intact)
    k_g = _group_scale(b, k, max_h)
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


def _edge_overflow(col, sizes, col_w, margin):
    """How far an image's transparent padding pokes out of its column, beyond the margin."""
    o = 0.0
    for b, sz in zip(col, sizes):
        if b.vis is not None:
            o = max(o, (b.w * sz[2] - col_w) / 2 - margin)
    return max(0.0, o)


def _fits(cols, k, W, H, gap, margin):
    """Check a column partition at scale k. Returns placement or None."""
    avail_w = W - 2 * margin - _RESERVE[0] - _RESERVE[1] - gap * (len(cols) - 1)
    avail_h = H - 2 * margin
    if avail_w <= 0 or avail_h <= 0:
        return None
    # fixed width per column = widest rigid block; flexible text gets remaining share
    fixed = []
    for col in cols:
        fw = max([(_block_size(b, k, 1e9, avail_h)[0] if (b.images and not b.texts and not b.shapes)
                   else b.w * _group_scale(b, k, avail_h))
                  for b in col if not b.flow_text] or [0])
        fixed.append(fw)
    flex_cols = [i for i, col in enumerate(cols) if any(b.flow_text for b in col)]
    rest = avail_w - sum(fixed[i] for i in range(len(cols)) if i not in flex_cols)
    # reserve canvas room for the outer columns' image padding before text claims it
    def _pre_over(col, fw_):
        o = 0.0
        for b in col:
            if b.vis is not None and not b.flow_text:
                sz = _block_size(b, k, 1e9, avail_h)
                o = max(o, (b.w * sz[2] - max(fw_, sz[0])) / 2 - margin)
        return max(0.0, o)
    if len(cols) > 1:  # a single centred column only needs the box to fit the canvas
        rest -= _pre_over(cols[0], fixed[0]) + _pre_over(cols[-1], fixed[-1])
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
    # the outer columns must leave room on the canvas for their images' invisible padding
    cw0 = [min(c, max(s[0] for s in sz) + 4) for c, (_, sz, _) in zip(widths, placed_cols)]
    o_l = _edge_overflow(cols[0], placed_cols[0][1], cw0[0], margin) if len(cols) > 1 else 0.0
    o_r = _edge_overflow(cols[-1], placed_cols[-1][1], cw0[-1], margin) if len(cols) > 1 else 0.0
    if sum(cw0) + o_l + o_r > avail_w + 1:
        return None
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
    if W / H >= 5:  # thin strips: give each piece clear breathing room
        gap = max(gap, W * 0.022)
    best = None
    max_cols = min(n, 5)
    # supporting text: a text block directly under another text block in the source
    # (headline -> subhead) should stay in the same column.
    src_h = blocks[0].ch
    keep_pairs = [
        (i, 0.85) for i in range(n - 1)
        if blocks[i].texts and blocks[i + 1].flow_text
        and 0 <= blocks[i + 1].y - (blocks[i].y + blocks[i].h) < 0.08 * src_h
        and abs((blocks[i].x + blocks[i].w / 2) - (blocks[i + 1].x + blocks[i + 1].w / 2)) < 0.15 * blocks[0].cw
    ]
    # a badge attached to the product must stay with it
    keep_pairs += [(i, 0.6) for i in range(n - 1) if blocks[i + 1].attach]
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


def _alignment_mode(blocks, cw):
    """How the original design lines its content up: 'left', 'right' or 'center'.
    A design counts as edge-aligned when its text is mostly aligned that way AND several
    units (text, pictures by their visible art, buttons) share that edge in the source."""
    texts = [b for b in blocks if b.flow_text]
    if not texts:
        return "center"
    aligns = [b.texts[0].get("text_align", "left") for b in texts]

    def edges(side):
        xs = []
        for b in blocks:
            v = b.vis or (0.0, 0.0, 1.0, 1.0)
            if side == "left":
                xs.append(b.x + v[0] * b.w)
            else:
                xs.append(b.x + v[2] * b.w)
        tol = 0.025 * cw
        return max(sum(1 for x2 in xs if abs(x2 - x1) <= tol) for x1 in xs)

    for side in ("left", "right"):
        n_side = sum(1 for a in aligns if a == side)
        if n_side > len(aligns) / 2 and edges(side) >= 3:
            return side
    return "center"


def _score_candidate(cols, part, k, m, W, H, gap, margin, keep_pairs):
    """Judge a candidate like a designer would, not just by size."""
    score = k * (1 - 0.02 * (m - 1))  # bigger is better; fewer columns break ties
    cuts = {a for a, _ in part[1:]}
    for i, pen in keep_pairs:  # related units (headline + subhead, product + badge) split apart
        if (i + 1) in cuts:
            score *= pen
    if m > 1:  # balanced columns read as intentional
        pl = _fits(cols, k * 0.999, W, H, gap, margin)
        if pl is not None:
            hs = [t for _, _, t in pl[1]]
            score *= 0.92 + 0.08 * (min(hs) / max(max(hs), 1e-6))
    return score


def _min_readable(W, H):
    return max(10.0, min(W, H) * 0.018)


# Placement

def _place(b: Block, x, y, w, h, k, out: dict):
    if b.flow_text:
        t = out[b.texts[0]["id"]]
        t["x"], t["y"], t["width"], t["height"] = x, y, w, h
        t["font_size"] = max(1.0, round(t["font_size"] * k * b.tf, 2))
        return
    if b.vis is not None:
        if b.sticker is None:
            x += (w - (b.vis[2] - b.vis[0]) * b.w * k) / 2  # centre the art in its slot
        x -= b.vis[0] * b.w * k
        y -= b.vis[1] * b.h * k
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


def _strip_row(row, out, W, H, min_px):
    """Thin strips read as one line: every piece shares the vertical centreline, the message
    gets enough width to wrap into as few lines as possible, the product fills the strip's
    height, and the button hugs its label. Applies only to one headline plus pictures and
    buttons; anything else keeps the general layout."""
    texts = [b for b in row if b.flow_text]
    if len(texts) != 1 or len(row) < 2:
        return
    if any(not (b.flow_text or (b.vis is not None) or (b.is_group and b.texts and b.shapes)) for b in row):
        return
    snapshot = {e["id"]: dict(out[e["id"]]) for b in row for e in b.els}
    pad_v = 0.1 * H
    items = []  # (block, width, place_fn)
    for b in row:
        if b.vis is not None:  # picture: product fills the height, logo keeps its size
            o = out[b.els[0]["id"]]
            v = b.vis
            if b.priority == 8.0:
                s_ = (H - 2 * pad_v) / max((v[3] - v[1]) * b.h, 1e-6)
                s_ = min(s_, H / max(b.h, 1e-6), W / max(b.w, 1e-6))
            else:  # logo: a touch smaller so the message gets the width
                s_ = 0.9 * o["width"] / max(b.w, 1e-6)
            items.append([b, (v[2] - v[0]) * b.w * s_, s_])
        elif b.is_group:  # button: label stays readable, the pill hugs it
            lab = next(e for e in b.els if e["type"] == "text")
            ol = out[lab["id"]]
            fs = max(ol["font_size"], min(min_px, ol["font_size"]))
            tw = _text_width(lab, fs, str(lab.get("content", "")))
            items.append([b, tw + 2.0 * fs, fs])
        else:
            items.append([b, 0.0, None])
    gap_min = 0.014 * W
    fixed = sum(it[1] for it in items)

    def edge(it, side):  # transparent padding of an end picture must stay on the canvas
        b_, w_, s_ = it
        if b_.vis is None:
            return 0.0
        v_ = b_.vis
        return (v_[0] if side == 0 else 1 - v_[2]) * b_.w * s_
    lead = max(gap_min, edge(items[0], 0))
    tail = max(gap_min, edge(items[-1], 1))
    lead = tail = max(lead, tail)  # equal visible safe margins on both ends
    t = texts[0]
    te = t.texts[0]
    f0 = out[te["id"]]["font_size"]
    best = None
    f = f0
    while f >= min_px - 1e-6:
        room = W - fixed - gap_min * (len(items) - 1) - lead - tail
        if room > 0:
            n, widest = _wrap_lines(te, f, room - (f * 0.5 + 2))
            hgt = n * f * float(te.get("line_height", LINE_HEIGHT))
            if hgt <= H - 2 * 4 and widest + f * 0.5 + 2 <= room:
                key = (n, -f)
                if best is None or key < best[0]:
                    best = (key, f, n, widest + f * 0.5 + 2, hgt)
        f -= 0.25
    if best is None:
        return
    _, fs_t, n_t, w_t, h_t = best
    for it in items:
        if it[0] is t:
            it[1] = w_t
    free = W - sum(it[1] for it in items) - lead - tail
    g = gap_min + max(0.0, free - gap_min * (len(items) - 1)) / (len(items) + 1)
    if free < gap_min * (len(items) - 1) - 0.5:
        for k_, v_ in snapshot.items():
            out[k_].update(v_)
        return
    x = lead + (g - gap_min)
    cy = H / 2
    for b, w_, extra in items:
        if b is t:
            o = out[te["id"]]
            o.update({"x": x, "width": w_, "font_size": round(fs_t, 2),
                      "height": h_t + fs_t * 0.25})
            o["y"] = cy - o["height"] / 2
        elif b.vis is not None:
            s_ = extra
            v = b.vis
            o = out[b.els[0]["id"]]
            o["width"], o["height"] = b.w * s_, b.h * s_
            o["x"] = x - v[0] * o["width"]
            o["y"] = cy - ((v[1] + v[3]) / 2) * o["height"]
        else:
            bg_ = next((e for e in b.els if e["type"] == "shape"), None)
            for e in b.els:
                o = out[e["id"]]
                ocx = o["y"] + o["height"] / 2
                o["x"], o["width"] = x, w_
                o["y"] += cy - (out[bg_["id"]]["y"] + out[bg_["id"]]["height"] / 2) if bg_ else 0
            if bg_ and out[bg_["id"]].get("corner_radius"):
                out[bg_["id"]]["corner_radius"] = min(out[bg_["id"]]["corner_radius"], out[bg_["id"]]["height"] / 2)
        x += w_ + g


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
    align_mode = _alignment_mode(blocks, cw)
    # thin strips (leaderboards, mobile banners) read left to right in one line: the logo
    # opens, the message follows, and the product sits right beside the call to action
    sticker = None
    if W / H >= 5:
        hero = next((b for b in blocks if b.images and not b.texts and b.priority == 8.0), None)
        cta = next((b for b in blocks if b.priority == 9.0 and b.is_group), None)

        def rotated(b):
            return any(abs(e.get("rotation", 0) or 0) > 1 for e in b.els)

        # one clickable-looking shape per strip: other pills and badges step aside
        for b in blocks:
            if b.is_group and b.texts and b.shapes and b is not cta and not rotated(b):
                b.dropped = b.forced = True
        # a rotated promo sticker ("SUNSET SALE") rides on the product, like a price tag
        st = next((b for b in blocks if b.is_group and b.texts and b.shapes and rotated(b)), None)
        if st is not None:
            fmin = max(min(t["font_size"] for t in st.texts), 1e-6)
            sc = max(8.0, 0.8 * _min_readable(W, H)) / fmin
            bw_, bh_ = _sticker_box(st, sc)
            if STICKER_ON_STRIPS and bw_ <= 0.2 * W and bh_ <= 0.75 * H:
                # it keeps the corner it had in the original design, like a corner flag
                # it tucks into the bottom-right corner, overlapping the button's corner a little
                st_right, st_top = True, False
                st.st_scale, sticker = sc, st
            blocks = [b for b in blocks if b is not st]
            if sticker is None:
                st.dropped = st.forced = True
                blocks.append(st)
        if hero is not None and cta is not None:
            moving = [hero] + [b for b in blocks if b.attach]
            rest = [b for b in blocks if b not in moving]
            i = rest.index(cta)
            blocks = rest[:i] + moving + rest[i:]

    # 1) background: full bleed, cover-crop
    for e in bgs:
        o = out[e["id"]]
        o.update({"x": 0, "y": 0, "width": W, "height": H})
        if e["type"] == "image":
            o["fit"] = "cover"

    # 2) content: search layouts, dropping lowest-priority blocks if text gets unreadable
    # never blow elements up beyond the overall change in canvas area
    cap = min(3.0, math.sqrt((W * H) / (cw * ch)) * 1.3)
    min_px = _min_readable(W, H)
    _IMG_BOOST[0] = 2.0 if W / H >= 3 else 1.0
    _MIN_PX[0] = min_px
    _CANVAS[0], _CANVAS[1] = W, H
    _RESERVE[0] = _RESERVE[1] = 0.0
    if sticker is not None:
        _RESERVE[1 if st_right else 0] = 0.6 * _sticker_box(sticker, sticker.st_scale)[0]
    active = [b for b in blocks if not b.dropped]

    def evaluate(act, tfac, relaxed=False):
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
        if relaxed:
            # restoring secondary content: key text keeps the full bar, the rest needs >= 8px
            sec_px = max(8.0, 0.8 * min_px)
            ok_ = all(t["font_size"] * scale.get(id(b), k_ * b.tf) >= (min_px if b.priority >= 8 else sec_px)
                      for b in act for t in b.texts)
        else:
            ok_ = (not fonts or min(fonts) >= min_px)
        if head and fonts:
            ok_ = ok_ and max(head) >= 0.9 * max(fonts)  # headline must stay the loudest text
        return res, ok_, (k_ * (0.85 + 0.15 * tfac) if ok_ else min(fonts or [0]))

    def search(act, relaxed=False):
        best_ = None
        for tfac in (1.0, 0.85, 0.7, 0.55, 0.45, 0.35):
            res, ok_, score = evaluate(act, tfac, relaxed)
            if res is None:
                continue
            if best_ is None or (ok_, score) > (best_[1], best_[2]):
                best_ = (res, ok_, score, tfac)
        return best_

    def hero_px(act, best_):
        # size of the loudest key text (headline / CTA) in a chosen layout
        res, _, _, tfac = best_
        k_ = res[0]
        scale = {}
        if res[2] is not None:
            for col, (_, sizes, _) in zip(res[1], res[2][1]):
                for b, sz in zip(col, sizes):
                    scale[id(b)] = sz[2] * (tfac if b.flow_text else 1.0)
        widths = {}
        if res[2] is not None:
            for col, (_, sizes, _) in zip(res[1], res[2][1]):
                for b, sz in zip(col, sizes):
                    widths[id(b)] = sz[0]
        px = max([t["font_size"] * scale.get(id(b), k_) for b in act for t in b.texts if b.priority >= 10] or [0])
        wd = max([widths.get(id(b), 0) for b in act if b.priority >= 10] or [0])
        return px, wd

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

    # restore: hide only what truly can't fit. Try to bring each dropped unit back, most
    # important first; keep it if its text can be >= 8px and the headline / CTA lose
    # no more than 10% of their size or line length.
    dropped = [b for b in blocks if b.dropped and not b.forced]
    if result is not None and dropped and ok:
        order = {id(b): i for i, b in enumerate(blocks)}
        cur = search(active)
        base_hero = hero_px(active, cur) if cur else (0, 0)
        for cand in sorted(dropped, key=lambda b: (-b.priority, b.y)):
            trial = sorted(active + [cand], key=lambda b: order[id(b)])
            got = search(trial, relaxed=True)
            hp = hero_px(trial, got) if got is not None else (0, 0)
            # headline keeps >= 90% of its size AND its line length (no squeezing into a column)
            if got is not None and got[1] and hp[0] >= 0.9 * base_hero[0] and hp[1] >= 0.9 * base_hero[1]:
                cand.dropped = False
                active = trial
                cur = got
        for b in active:
            b.tf = cur[3] if b.flow_text else 1.0
        result = cur[0]

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
        free_w = W - 2 * margin - _RESERVE[0] - _RESERVE[1] - total_w
        # even gaps between columns, capped so the composition stays a tight group;
        # whatever is left over becomes equal outer margins (group is centred)
        o_l = _edge_overflow(cols[0], placed_cols[0][1], widths[0], margin) if len(cols) > 1 else 0.0
        o_r = _edge_overflow(cols[-1], placed_cols[-1][1], widths[-1], margin) if len(cols) > 1 else 0.0
        if len(cols) > 1:
            extra = min(free_w / (len(cols) + 1), max(gap * 1.5, W * 0.03),
                        max(0.0, (free_w - o_l - o_r) / (len(cols) - 1)))
            rest_ = free_w - extra * (len(cols) - 1)
        else:
            extra, rest_ = 0.0, free_w
        lead = min(max(rest_ / 2, o_l), max(o_l, rest_ - o_r))
        x = margin + _RESERVE[0] + lead
        for col, (col_w, sizes, total_h) in zip(cols, placed_cols):
            # vertical: spread blocks across available height (designed breathing room)
            avail_h = H - 2 * margin
            free_h = avail_h - total_h
            # a headline and the smaller text line right under it read as one unit: keep them
            # snug; the breathing room goes between the other pieces instead
            tight = [i for i in range(len(col) - 1)
                     if col[i].flow_text and col[i + 1].flow_text
                     and col[i].max_font >= 1.2 * col[i + 1].max_font
                     # ...only if they sat right against each other in the original design
                     and col[i + 1].y - (col[i].y + col[i].h) < 0.03 * col[i].ch]
            loose = len(col) - 1 - len(tight)
            snug = gap * 0.6
            inner = gap * 0.6
            if loose and free_h > 0:
                inner += min(free_h / (loose + 2), gap * 2.5)
            gaps_ = [snug if i in tight else inner for i in range(len(col) - 1)]
            used = sum(s[1] for s in sizes) + sum(gaps_)
            y = margin + (avail_h - used) / 2
            for i_b, (b, (bw, bh, bs)) in enumerate(zip(col, sizes)):
                inner = gaps_[i_b] if i_b < len(gaps_) else 0.0
                if align_mode == "left":      # keep the original's shared left edge
                    bx = x
                elif align_mode == "right":   # ...or its shared right edge
                    bx = x + col_w - bw
                else:
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

    # images whose transparent padding would poke past the canvas edge are shrunk about
    # their own centre (never shoved sideways into a neighbour); proportions are kept
    for b in blocks:
        if b.vis is None or b.dropped:
            continue
        o = out[b.els[0]["id"]]
        cx, cy = o["x"] + o["width"] / 2, o["y"] + o["height"] / 2
        f = min(1.0, 2 * cx / max(o["width"], 1e-6), 2 * (W - cx) / max(o["width"], 1e-6),
                2 * cy / max(o["height"], 1e-6), 2 * (H - cy) / max(o["height"], 1e-6))
        if f < 1.0:
            f *= 0.999
            o["width"], o["height"] = o["width"] * f, o["height"] * f
            o["x"], o["y"] = cx - o["width"] / 2, cy - o["height"] / 2

    # 3) decorations. A decoration that overlapped content in the source stays with that
    #    content: full-width/height bands keep spanning the canvas behind the blocks they
    #    covered; other shapes keep their spot relative to the element they sat behind.
    def _src_box(b):
        return b.x, b.y, b.w, b.h

    def _out_box(b):
        xs = [out[e["id"]] for e in b.els]
        x0 = min(o_["x"] for o_ in xs); y0 = min(o_["y"] for o_ in xs)
        x1 = max(o_["x"] + o_["width"] for o_ in xs); y1 = max(o_["y"] + o_["height"] for o_ in xs)
        return x0, y0, x1 - x0, y1 - y0

    def _ov(a, b):
        ix = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
        iy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
        return max(0.0, ix) * max(0.0, iy)

    def _art_box(b):
        x_, y_, w_, h_ = _out_box(b)
        if b.vis is not None:
            v = b.vis
            return x_ + v[0] * w_, y_ + v[1] * h_, (v[2] - v[0]) * w_, (v[3] - v[1]) * h_
        return x_, y_, w_, h_

    live = [b for b in blocks if not b.dropped]
    anchored = set()
    for e in decos:
        if e["type"] != "shape" or k is None or not live:
            continue
        eb = (e["x"], e["y"], e["width"], e["height"])
        o = out[e["id"]]
        band_w = e["width"] >= 0.9 * cw
        band_h = e["height"] >= 0.9 * ch
        if band_w or band_h:
            covered = [b for b in live if _ov(eb, _src_box(b)) >= 0.5 * max(b.w * b.h, 1e-6)]
            if not covered:
                continue
            boxes = [_out_box(b) for b in covered]
            pad = 0.04 * min(W, H)
            if band_w:
                y0 = max(0.0, min(bx[1] for bx in boxes) - pad)
                y1 = min(H, max(bx[1] + bx[3] for bx in boxes) + pad)
                if e["y"] + e["height"] >= ch - 2:   # band ran to the bottom edge: keep that
                    y1 = H
                if e["y"] <= 2:                      # ...or to the top edge
                    y0 = 0.0
                o.update({"x": 0.0, "y": y0, "width": W, "height": y1 - y0})
            else:
                x0 = max(0.0, min(bx[0] for bx in boxes) - pad)
                x1 = min(W, max(bx[0] + bx[2] for bx in boxes) + pad)
                if e["x"] + e["width"] >= cw - 2:
                    x1 = W
                if e["x"] <= 2:
                    x0 = 0.0
                o.update({"x": x0, "y": 0.0, "width": x1 - x0, "height": H})
            anchored.add(e["id"])
            continue
        best_b, best_ov = None, 0.0
        for b in live:
            if b.flow_text:  # decorations belong behind pictures / buttons, never behind body text
                continue
            v = _ov(eb, _src_box(b))
            if v > best_ov:
                best_b, best_ov = b, v
        if best_b is None:
            continue
        sx0, sy0, sw0, sh0 = _src_box(best_b)
        ox0, oy0, ow0, oh0 = _out_box(best_b)
        ka = ow0 / max(sw0, 1e-6)
        # where the decoration's centre sat on its anchor (clamped to the anchor's box)
        fx = min(1.0, max(0.0, (e["x"] + e["width"] / 2 - sx0) / max(sw0, 1e-6)))
        fy = min(1.0, max(0.0, (e["y"] + e["height"] / 2 - sy0) / max(sh0, 1e-6)))
        w_, h_ = e["width"] * ka, e["height"] * ka
        f_ = min(1.0, W / max(w_, 1), H / max(h_, 1))
        w_, h_ = w_ * f_, h_ * f_
        # never behind the logo: slide toward the anchor's centre until the logo is clear
        if best_b.vis is not None:  # a glow behind a picture is centred on the visible artwork
            ab = _art_box(best_b)
            fx = (ab[0] + ab[2] / 2 - ox0) / max(ow0, 1e-6)
            fy = (ab[1] + ab[3] / 2 - oy0) / max(oh0, 1e-6)
        logo_art = [_art_box(b) for b in live if b.priority >= 9.5 and b is not best_b]
        for t_ in [i_ / 10 for i_ in range(11)]:
            gx = fx + (0.5 - fx) * t_
            gy = fy + (0.5 - fy) * t_
            cand = (ox0 + gx * ow0 - w_ / 2, oy0 + gy * oh0 - h_ / 2, w_, h_)
            if not any(_ov(cand, la) > 0 for la in logo_art):
                break
        # still touching the logo? shrink the glow about its centre until there is clear space
        cx_, cy_ = cand[0] + w_ / 2, cand[1] + h_ / 2
        f_ = 1.0
        while f_ > 0.6 and any(_ov((cx_ - w_ * f_ / 2 - 6, cy_ - h_ * f_ / 2 - 6, w_ * f_ + 12, h_ * f_ + 12), la) > 0
                               for la in logo_art):
            f_ -= 0.05
        w_, h_ = w_ * f_, h_ * f_
        o.update({"width": w_, "height": h_, "x": cx_ - w_ / 2, "y": cy_ - h_ / 2})
        anchored.add(e["id"])

    for e in decos:
        if e["id"] in anchored:
            continue
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

    if W / H >= 5 and result is not None:
        _strip_row([b for b in blocks if not b.dropped], out, W, H, min_px)

    if sticker is not None:
        sc = sticker.st_scale
        fw, fh = _sticker_box(sticker, sc)
        inset = 0.04 * H
        gx = (W - inset - fw / 2) if st_right else (inset + fw / 2)
        gy = (inset + fh / 2) if st_top else (H - inset - fh / 2)
        for e in sticker.els:
            o = out[e["id"]]
            o["width"], o["height"] = e["width"] * sc, e["height"] * sc
            o["x"] = gx - sticker.w * sc / 2 + (e["x"] - sticker.x) * sc
            o["y"] = gy - sticker.h * sc / 2 + (e["y"] - sticker.y) * sc
            if e["type"] == "text":
                o["font_size"] = max(1.0, e["font_size"] * sc)
            if e.get("corner_radius"):
                o["corner_radius"] = e["corner_radius"] * sc

    for o in out.values():
        _clamp(o, W, H)

    elements = [out[e["id"]] for e in source["elements"]]
    return {"canvas": {"width": target_canvas["width"], "height": target_canvas["height"]}, "elements": elements}