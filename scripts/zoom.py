#!/usr/bin/env python3
"""On-demand magnified view of a region of one view's object grid.

The inspection workflow needs the ability to look closely at a suspicious
spot without pre-generating every zoom level.  This script turns normalised
coordinates (as reported in the manifest / the model's findings) into a
high-resolution JPEG crop.

Coordinates are normalised to the *object grid* of the view: (0,0) is the
top-left of the cropped object, (1,1) the bottom-right.

Examples
--------
    python zoom.py --work work --item item-p17 --view view-01 \
        --x 0.62 --y 0.35 --span 0.20 --out _t/zoom.jpg

    python zoom.py --work work --item item-p17 --view view-01 --tile r1c3 \
        --out _t/tile.jpg

    python zoom.py --work work --item item-p17 --view view-01 \
        --x 0.62 --y 0.35 --span 0.15 --views view-02,view-03 --out _t/z
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖，请先执行: pip install opencv-python numpy")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare import save_jpg  # noqa: E402


def load_manifest(work):
    p = Path(work) / "manifest.json"
    if not p.exists():
        sys.exit(f"找不到 {p}，请先运行 prepare.py")
    return json.loads(p.read_text(encoding="utf-8"))


def find_view(manifest, item, view):
    it = manifest["items"].get(item)
    if it is None:
        sys.exit(f"manifest 中没有 {item}")
    for v in it["views"]:
        if v["view"] == view:
            return it, v
    sys.exit(f"{item} 中没有视图 {view}")


def crop_region(view, x, y, span, out_side=1024):
    it_dir = Path(view["_dir"])
    grid = cv2.imread(str(it_dir / view["view"] / "object.jpg"))
    if grid is None:
        sys.exit("找不到 object.jpg，请先运行 prepare.py")
    h, w = grid.shape[:2]
    side = max(24, int(span * max(h, w)))
    cx, cy = int(x * w), int(y * h)
    x0 = max(0, min(w - side, cx - side // 2))
    y0 = max(0, min(h - side, cy - side // 2))
    x1 = min(w, x0 + side)
    y1 = min(h, y0 + side)
    crop = grid[y0:y1, x0:x1]
    if max(crop.shape[:2]) < out_side:
        s = out_side / float(max(crop.shape[:2]))
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * s)),
                                 max(1, int(crop.shape[0] * s))),
                          interpolation=cv2.INTER_CUBIC)
    return crop, [x0, y0, x1 - x0, y1 - y0], [w, h]


def main():
    ap = argparse.ArgumentParser(description="magnified crop of one region")
    ap.add_argument("--work", default="work")
    ap.add_argument("--item", required=True)
    ap.add_argument("--view", required=True, help="view-01 ...")
    ap.add_argument("--x", type=float, help="normalised centre x in the object grid")
    ap.add_argument("--y", type=float, help="normalised centre y in the object grid")
    ap.add_argument("--span", type=float, default=0.2,
                    help="window width as a fraction of the object grid max side")
    ap.add_argument("--views", default="", help="extra views, same normalised region")
    ap.add_argument("--tile", default="", help="tile id like r1c3 instead of x/y")
    ap.add_argument("--out-side", type=int, default=1024)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    manifest = load_manifest(args.work)
    it, view = find_view(manifest, args.item, args.view)
    view["_dir"] = it["dir"]

    targets = [args.view]
    if args.views:
        targets += [v.strip() for v in args.views.split(",") if v.strip()]

    written = []
    for tv in targets:
        if tv not in {v["view"] for v in it["views"]}:
            print(f"[warn] {args.item} 没有视图 {tv}", file=sys.stderr)
            continue
        tview = next(v for v in it["views"] if v["view"] == tv)
        tview["_dir"] = it["dir"]
        if args.tile:
            hit = next((t for t in tview["tiles"]
                        if Path(t["file"]).stem == f"tile-{args.tile}"), None)
            if hit is None:
                print(f"[warn] {args.item}/{tv} 没有瓦片 {args.tile}", file=sys.stderr)
                continue
            path = Path(it["dir"]) / tv / hit["file"]
            img = cv2.imread(str(path))
            meta = {"tile": args.tile, "bbox_in_object": hit["bbox_in_object"]}
        else:
            img, bbox, gsize = crop_region(tview, args.x, args.y, args.span,
                                           args.out_side)
            meta = {"bbox_in_object": bbox, "object_grid_size": gsize}
        base = args.out or str(Path(args.work) / args.item / f"zoom-{tv}")
        out = Path(base)
        if out.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            out = out.with_suffix(".jpg")
        info = save_jpg(out, img)
        meta.update({"view": tv, "out": str(out), "image": info})
        written.append(meta)
        print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
