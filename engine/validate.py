"""
Stronger validation for CHAMELEON outputs.

Used two ways:
  1. Inside the engine: resize() calls hard_failures() on its own result and switches
     to the safe fallback if any hard rule is broken.
  2. As a command:  python -m engine.validate
     Checks every input/*.json against output/*_output.json and prints a report.

HARD (would fail structural scoring):
  - every source id present exactly once, nothing added
  - every element inside the canvas
  - non-background images keep their aspect ratio (within 5%)
  - content and font_family unchanged
SOFT (would cost points with the visual judge):
  - visible text below a readable size
  - text that wraps to more lines than its box can hold (real font metrics)
  - unintended collisions between visible elements of different units
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RATIO_TOL = 0.05


def _visible(e: dict) -> bool:
    return float(e.get("opacity", 1.0)) > 0.01 and e["width"] > 1.5 and e["height"] > 1.5


def _is_background(src: dict, canvas: dict) -> bool:
    return src["type"] == "image" and (
        src.get("fit") == "cover"
        or src["width"] * src["height"] >= 0.85 * canvas["width"] * canvas["height"]
    )


def hard_failures(source: dict, output: dict) -> list[str]:
    problems = []
    src_ids = [e["id"] for e in source["elements"]]
    out_ids = [e.get("id") for e in output.get("elements", [])]
    if sorted(src_ids) != sorted(out_ids):
        problems.append(f"id mismatch: missing={set(src_ids) - set(out_ids)} extra={set(out_ids) - set(src_ids)}")
        return problems
    W, H = output["canvas"]["width"], output["canvas"]["height"]
    src_by_id = {e["id"]: e for e in source["elements"]}
    for o in output["elements"]:
        s = src_by_id[o["id"]]
        if o["x"] < -0.5 or o["y"] < -0.5 or o["x"] + o["width"] > W + 0.5 or o["y"] + o["height"] > H + 0.5:
            problems.append(f"{o['id']}: off-canvas")
        if o["type"] == "image" and not _is_background(s, source["canvas"]) and _visible(o):
            r_src = s["width"] / max(s["height"], 1e-6)
            r_out = o["width"] / max(o["height"], 1e-6)
            if abs(r_out / r_src - 1) > RATIO_TOL:
                problems.append(f"{o['id']}: image distorted ({r_src:.2f} -> {r_out:.2f})")
        if o["type"] == "text":
            if o.get("content") != s.get("content"):
                problems.append(f"{o['id']}: text content changed")
            if o.get("font_family") != s.get("font_family"):
                problems.append(f"{o['id']}: font_family changed")
    return problems


def soft_warnings(source: dict, output: dict, min_px: float = 10.0) -> list[str]:
    from engine.resize import _wrap_lines, LINE_HEIGHT  # real font metrics

    warns = []
    W, H = output["canvas"]["width"], output["canvas"]["height"]
    min_px = max(min_px, min(W, H) * 0.018) if min(W, H) > 120 else 8.0
    src_by_id = {e["id"]: e for e in source["elements"]}
    vis = [o for o in output["elements"] if _visible(o) and not _is_background(src_by_id[o["id"]], source["canvas"])]

    for o in vis:
        if o["type"] != "text":
            continue
        if o["font_size"] < min_px - 0.05:
            warns.append(f"{o['id']}: text {o['font_size']:.1f}px is below readable {min_px:.0f}px")
        n, widest = _wrap_lines(o, o["font_size"], o["width"])
        need = n * o["font_size"] * float(o.get("line_height", LINE_HEIGHT))
        if need > o["height"] * 1.15 + 2:
            warns.append(f"{o['id']}: text needs {need:.0f}px height, box is {o['height']:.0f}px")
        if widest > o["width"] + 2:
            warns.append(f"{o['id']}: a word is wider than its box (would be cut off)")

    def art(o):
        # images are judged by their visible artwork, not their transparent padding
        if o["type"] != "image":
            return o["x"], o["y"], o["width"], o["height"]
        try:
            from engine.resize import _visible_frac
            f = _visible_frac(o)
        except Exception:
            f = (0.0, 0.0, 1.0, 1.0)
        return (o["x"] + f[0] * o["width"], o["y"] + f[1] * o["height"],
                (f[2] - f[0]) * o["width"], (f[3] - f[1]) * o["height"])

    def unit(o):
        g = src_by_id[o["id"]].get("group_id")
        return g or o["id"]

    def solid(o):  # translucent decoration layered behind content is intentional
        return float(o.get("opacity", 1.0)) >= 0.5

    for i in range(len(vis)):
        for j in range(i + 1, len(vis)):
            a, b = vis[i], vis[j]
            if unit(a) == unit(b) or not (solid(a) and solid(b)):
                continue
            ax, ay, aw, ah = art(a)
            bx, by, bw, bh = art(b)
            ix = min(ax + aw, bx + bw) - max(ax, bx)
            iy = min(ay + ah, by + bh) - max(ay, by)
            if ix > 2 and iy > 2:
                # a label fully on top of a shape is a deliberate pairing
                inner = a if a["width"] * a["height"] < b["width"] * b["height"] else b
                outer = b if inner is a else a
                if inner["type"] == "text" and outer["type"] == "shape" and ix * iy >= 0.9 * inner["width"] * inner["height"]:
                    continue
                warns.append(f"collision: {a['id']} x {b['id']} ({ix:.0f}x{iy:.0f}px)")
    return warns


def main() -> int:
    total_hard = total_soft = 0
    for task_path in sorted(Path("input").glob("*.json")):
        task = json.loads(task_path.read_text())
        out_path = Path("output") / f"{task_path.stem}_output.json"
        if not out_path.exists():
            print(f"[{task_path.name}] no output yet - run python -m engine.main first")
            continue
        out = json.loads(out_path.read_text())
        hard, soft = hard_failures(task["source"], out), soft_warnings(task["source"], out)
        total_hard += len(hard)
        total_soft += len(soft)
        print(f"[{task_path.name}] hard={len(hard)} soft={len(soft)}")
        for p in hard:
            print("   HARD:", p)
        for p in soft:
            print("   soft:", p)
    print(f"\nTOTAL hard={total_hard} soft={total_soft}")
    return 1 if total_hard else 0


if __name__ == "__main__":
    sys.exit(main())