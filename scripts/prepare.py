#!/usr/bin/env python3
"""Prepare multi-view product photos for surface-defect inspection.

Pipeline per view
------------------
1. foreground segmentation (background is a near-uniform studio backdrop)
   -> object mask; the background is then inpainted away so the silhouette
      edge does not swamp the anomaly response.
2. crop to the object and resample into the *object grid*: the canonical
   coordinate frame of one view.  Small objects are upscaled so the model
   receives enough pixels to work with.
3. noise-normalised proposal generator
      z_blob = |gray - median(gray,K)| / sigma      -> pits, chips, exposed material
      z_line = multi-scale black-hat / white-hat / sigma -> scratches, abrasions
   sigma is the robust (MAD) noise level of the object surface, so the
   response is comparable across lighting/material.
4. rendering
      object.jpg    object grid, full resolution          (model input)
      anomaly.jpg   object grid + numbered candidate boxes (model input)
      tiles/*.jpg   overlapping 2.7x magnified tiles       (deep scan)
      cands/*.jpg   magnified candidate crops             (adjudication)
   per item: overview.jpg (all views), candsheet.jpg (top candidate crops)

All model-facing images are JPEG under a byte budget: viewers (including
Claude Code's Read tool) silently downsample large images, which destroys the
magnification the detection depends on.

Usage
-----
    python prepare.py --input images --work work
    python prepare.py --input images --work work --items item-p01,item-p17
    python prepare.py --input images --work work --deep        # 5x tiles
    python prepare.py --input images --work work --debug-overlay
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖，请先执行: pip install opencv-python numpy")

MAX_BYTES = 130_000
# a patch of exposed substrate has to differ from its surroundings by this
# many robust noise units before it is proposed as missing material
MAT_Z = 8.0


# --------------------------------------------------------------------------
# image output with a byte budget
# --------------------------------------------------------------------------

def save_jpg(path, img, max_bytes=MAX_BYTES, quality=92):
    """Write img as JPEG.  Quality is reduced before resolution so that the
    consumer never has to downsample the image."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    h, w = img.shape[:2]
    buf = b""
    for _ in range(9):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok and len(buf) <= max_bytes:
            break
        if quality > 62:
            quality -= 8
        else:
            scale = 0.82
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                             interpolation=cv2.INTER_AREA)
            w, h = img.shape[1], img.shape[0]
            quality = 88
    path.write_bytes(buf.tobytes())
    return {"file": path.name, "w": w, "h": h, "bytes": len(buf), "quality": quality}


def _resize_max(img, max_side):
    h, w = img.shape[:2]
    if max(h, w) <= max_side:
        return img
    scale = max_side / float(max(h, w))
    return cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                      interpolation=cv2.INTER_AREA)


def _resize_up(img, min_side):
    h, w = img.shape[:2]
    if min(h, w) >= min_side:
        return img
    scale = min_side / float(min(h, w))
    return cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                      interpolation=cv2.INTER_CUBIC)


# --------------------------------------------------------------------------
# foreground segmentation
# --------------------------------------------------------------------------

def _ring_pixels(img, frac=0.015, min_px=6):
    h, w = img.shape[:2]
    b = max(min_px, int(frac * min(h, w)))
    parts = [img[:b], img[-b:], img[:, :b], img[:, -b:]]
    return np.concatenate([p.reshape(-1, p.shape[-1]) for p in parts], axis=0)


def _fill_holes(mask):
    h, w = mask.shape
    ff = mask.copy()
    pad = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(ff, pad, (0, 0), 1)
    holes = (ff == 0).astype(np.uint8)
    return ((mask | holes) > 0).astype(np.uint8)


def _edge_fallback(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 30, 90)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k)
    edges = cv2.morphologyEx(edges, cv2.MORPH_DILATE, k)
    ff = edges.copy()
    ff[0, 0] = 1
    h2 = np.zeros((gray.shape[0] + 2, gray.shape[1] + 2), np.uint8)
    cv2.floodFill(ff, h2, (0, 0), 0)
    mask = (ff == 0).astype(np.uint8)
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n > 1:
        idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = (lbl == idx).astype(np.uint8)
    return mask


def _multi_modal_threshold(dist8, k=3):
    """Split the distance histogram into k modes and return the boundary
    between the two lowest ones.

    The distance-from-backdrop histogram of a studio shot is usually
    multi-modal: a huge backdrop peak at 0, a near-background material
    (white foam, unpainted plastic), and the saturated part.  Plain Otsu
    separates the two *brightest* modes, so the near-background material is
    thrown away together with the backdrop and the object is cut down to its
    saturated parts only.  Cutting between the two *lowest* modes keeps
    every material that is actually on the object.
    """
    hist = np.bincount(dist8.ravel(), minlength=256).astype(np.float64)
    vals = np.arange(256, dtype=np.float64)
    # seed the modes at the strongest peaks of a smoothed histogram
    smooth = cv2.GaussianBlur(hist.astype(np.float32).reshape(-1, 1), (1, 9), 0).ravel()
    work = smooth.copy()
    centres = []
    for _ in range(k):
        i = int(np.argmax(work))
        centres.append(float(i))
        work[max(0, i - 8):i + 9] = 0.0
    centres = np.array(sorted(centres), dtype=np.float64)
    for _ in range(60):
        assign = np.abs(vals[:, None] - centres[None, :]).argmin(axis=1)
        new = np.array([
            (vals[assign == i] * hist[assign == i]).sum() / hist[assign == i].sum()
            if hist[assign == i].sum() > 0 else centres[i]
            for i in range(len(centres))
        ])
        if np.allclose(new, centres, atol=1e-3):
            break
        centres = new
    return float((centres[0] + centres[1]) / 2.0)


def segment_object(img_bgr):
    h, w = img_bgr.shape[:2]
    ring = _ring_pixels(img_bgr)
    bg = np.median(ring, axis=0)
    bg_lab = cv2.cvtColor(bg.astype(np.uint8).reshape(1, 1, 3), cv2.COLOR_BGR2LAB)
    bg_lab = bg_lab.astype(np.float32)[0, 0]

    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    dist = np.sqrt(((lab - bg_lab) ** 2).sum(axis=2))
    dist8 = np.clip(dist, 0, 255).astype(np.uint8)

    otsu, _ = cv2.threshold(dist8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = max(float(otsu), 14.0)
    # a near-background material (e.g. a white foam sleeve on a white
    # backdrop) sits between the backdrop peak and the saturated material;
    # keep it, otherwise the whole sleeve is segmented away and every
    # defect on it is invisible
    thr = min(thr, max(_multi_modal_threshold(dist8), 14.0))
    mask = (dist8 >= thr).astype(np.uint8)

    k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k5)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k5)

    n, lbl, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        keep = np.where(areas >= max(24, 0.02 * areas.max()))[0] + 1
        mask = np.isin(lbl, keep).astype(np.uint8)

    if mask.mean() < 0.0005 or mask.mean() > 0.985:
        mask = _edge_fallback(img_bgr)

    filled = _fill_holes(mask)
    ys, xs = np.nonzero(filled)
    if xs.size == 0:
        return filled, {"ok": False, "reason": "empty mask"}
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    return filled, {
        "ok": True,
        "bg_rgb": [int(v) for v in bg],
        "threshold": round(thr, 2),
        "area_frac": round(float(filled.mean()), 5),
        "bbox": [x0, y0, x1 - x0 + 1, y1 - y0 + 1],
    }


# --------------------------------------------------------------------------
# noise normalised proposal generator
# --------------------------------------------------------------------------

def _components(binary, min_area=8, zmap=None):
    num, lbl, stats, _ = cv2.connectedComponentsWithStats(binary.astype(np.uint8), 8)
    out = []
    # one pass over the labelled pixels instead of a full-image scan per
    # component: the number of components grows with the candidate count and
    # the per-component scan dominated the runtime
    ys, xs = np.nonzero(lbl)
    if ys.size == 0:
        return out
    flat = lbl[ys, xs]
    order = np.argsort(flat, kind="stable")
    ys, xs, flat = ys[order], xs[order], flat[order]
    starts = np.searchsorted(flat, np.arange(num + 1))
    for i in range(1, num):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        sl = slice(starts[i], starts[i + 1])
        cys, cxs = ys[sl], xs[sl]
        if cxs.size == 0:
            continue
        pts = np.stack([cxs, cys], axis=1).astype(np.float32)
        mean = pts.mean(axis=0)
        if len(pts) < 3:
            length = float(stats[i, cv2.CC_STAT_WIDTH])
            width = float(stats[i, cv2.CC_STAT_HEIGHT])
        else:
            try:
                cov = np.cov((pts - mean).T)
                evals, evecs = np.linalg.eigh(cov)
                pm = (pts - mean) @ evecs[:, -1]
                pn = (pts - mean) @ evecs[:, 0]
                length = float(pm.max() - pm.min())
                width = float(pn.max() - pn.min())
            except np.linalg.LinAlgError:
                length = float(stats[i, cv2.CC_STAT_WIDTH])
                width = float(stats[i, cv2.CC_STAT_HEIGHT])
        c = {
            "area": area,
            "length": length,
            "width": width,
            "elong": length / max(width, 1.0),
            "cx": float(mean[0]),
            "cy": float(mean[1]),
            "x": int(stats[i, cv2.CC_STAT_LEFT]),
            "y": int(stats[i, cv2.CC_STAT_TOP]),
            "w": int(stats[i, cv2.CC_STAT_WIDTH]),
            "h": int(stats[i, cv2.CC_STAT_HEIGHT]),
        }
        if zmap is not None:
            # statistics over the component's own pixels only: a bbox mean
            # would be diluted by the surrounding surface
            vals = zmap[cys, cxs]
            c["z"] = round(float(np.percentile(vals, 75)), 1)
            c["zmax"] = round(float(vals.max()), 1)
        out.append(c)
    return out


def estimate_sigma(gray, mask):
    """Robust noise level of the object *surface*.

    The mask is eroded first: the silhouette and the edges of holes would
    otherwise dominate the high-pass statistics and inflate sigma, which
    would in turn hide faint surface defects.
    """
    inner = cv2.erode(mask, np.ones((9, 9), np.uint8))
    if inner.sum() < 0.2 * max(1, mask.sum()):
        inner = mask
    hp = gray.astype(np.float32) - cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 2.0)
    vals = hp[inner > 0]
    if vals.size < 100:
        vals = hp.ravel()
    mad = float(np.median(np.abs(vals - np.median(vals))))
    return max(1.4826 * mad, 0.8)


def _material_response(lab, mask):
    """Colour deviation of each pixel from its surroundings, in units of the
    surface's own colour noise.

    Large neighbourhoods are compared against the local colour so that a patch
    of exposed substrate -- which differs from the host surface in *hue*, not
    in brightness alone -- produces a solid response over the whole patch.
    """
    inner = cv2.erode(mask, np.ones((9, 9), np.uint8))
    vals = []
    dev = np.zeros(lab.shape[:2], np.float32)
    for k in (31, 61, 101):
        med = np.stack([cv2.medianBlur(lab[:, :, c], k).astype(np.float32)
                        for c in range(3)], axis=2)
        d = np.sqrt(((lab.astype(np.float32) - med) ** 2).sum(axis=2))
        np.maximum(dev, d, out=dev)
        sel = d[inner > 0]
        if sel.size:
            vals.append(sel)
    if not vals:
        return dev
    allv = np.concatenate(vals)
    med = float(np.median(allv))
    mad = float(np.median(np.abs(allv - med)))
    sigma = max(1.4826 * mad, 0.5)
    return dev / sigma


def propose(gray, mask, sigma, lab=None):
    """Return candidate regions, split into line-like and blob-like."""
    g = gray.astype(np.float32)
    g8 = gray
    # the silhouette band is a strong, systematic response; shrink the mask
    # proportionally to the object size so it does not flood the proposals
    margin = int(max(5, min(30, 0.014 * max(mask.shape))))
    inner = cv2.erode(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                       (2 * margin + 1, 2 * margin + 1)))

    # ---- line response: multi scale black-hat (dark line) / white-hat (bright line)
    resp_d = np.zeros_like(g)
    resp_l = np.zeros_like(g)
    for k in (7, 15, 31):
        se = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        close = cv2.morphologyEx(g8, cv2.MORPH_CLOSE, se).astype(np.float32)
        openm = cv2.morphologyEx(g8, cv2.MORPH_OPEN, se).astype(np.float32)
        np.maximum(resp_d, close - g, out=resp_d)
        np.maximum(resp_l, g - openm, out=resp_l)
    z_line = np.maximum(resp_d, resp_l) / sigma

    # ---- blob response: median background removes thin lines, keeps blobs
    z_blob = np.zeros_like(g)
    for med_k in (13, 21):
        med = cv2.medianBlur(g8, med_k).astype(np.float32)
        np.maximum(z_blob, np.abs(g - med) / sigma, out=z_blob)

    results = []

    bin_line = ((z_line >= 4.5) & (inner > 0)).astype(np.uint8)
    bin_line = cv2.morphologyEx(bin_line, cv2.MORPH_OPEN, np.ones((1, 3), np.uint8))
    bin_line = cv2.morphologyEx(bin_line, cv2.MORPH_OPEN, np.ones((3, 1), np.uint8))
    for c in _components(bin_line, min_area=8, zmap=z_line):
        if c["elong"] < 2.5 or c["length"] < 12:
            continue
        if c["area"] > 4000:
            continue
        z = c["z"]
        # very long responses are usually structural edges, not surface marks
        prior = 1.0 if 30 <= c["length"] <= 400 else 0.6
        score = min(1.0, z * prior / 30.0)
        results.append(dict(type="scratch", subtype="line", score=round(score, 3), **c))

    bin_blob = ((z_blob >= 6.0) & (inner > 0)).astype(np.uint8)
    bin_blob = cv2.morphologyEx(bin_blob, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    for c in _components(bin_blob, min_area=12, zmap=z_blob):
        if c["elong"] > 4.0 or c["length"] < 3:
            continue
        if c["w"] < 4 or c["h"] < 4:
            continue
        if c["area"] > 9000:
            continue
        z = c["z"]
        # a defect is a local imperfection: both specks and huge smears are
        # less likely to be one
        prior = 1.0 if 30 <= c["area"] <= 1500 else 0.6
        if c["elong"] > 2.0:
            prior *= 0.75
        score = min(1.0, z * prior / 30.0)
        results.append(dict(type="chip", subtype="blob", score=round(score, 3), **c))

    # ---- missing material along the outline
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    notch = ((closed > 0) & (mask == 0)).astype(np.uint8)
    for c in _components(notch, min_area=10):
        if c["area"] > 5000:
            continue
        score = min(1.0, (0.55 * min(c["area"], 1000) / 1000.0 + 0.45) * 0.7)
        results.append(dict(type="chip", subtype="notch", score=round(score, 3),
                            z=0.0, **c))

    # ---- missing material: a patch of exposed substrate
    # A chip that reaches the material underneath shows up as a region whose
    # colour differs from the surrounding surface over a *large* area.  The
    # blob response above uses 13/21 px median filters, which are far too
    # small to see such a patch: the median window fits inside the patch, so
    # the response vanishes exactly where the defect is.  Compare against
    # progressively larger neighbourhoods instead and keep the strongest.
    if lab is not None:
        z_mat = _material_response(lab, mask)
        bin_mat = ((z_mat >= MAT_Z) & (inner > 0)).astype(np.uint8)
        bin_mat = cv2.morphologyEx(bin_mat, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        for c in _components(bin_mat, min_area=60, zmap=z_mat):
            if c["elong"] > 3.0:
                continue
            if c["w"] < 10 or c["h"] < 10:
                continue
            if c["area"] > 12000:
                continue
            z = c["z"]
            # a genuine chip is a compact patch of a different material, not
            # the long interface where two designed materials meet
            prior = 1.0 if 150 <= c["area"] <= 4000 else 0.6
            score = min(1.0, z * prior / 25.0)
            results.append(dict(type="chip", subtype="missing_material",
                                score=round(score, 3), **c))
    return results


def _iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / ua if ua > 0 else 0.0


def nms(cands, iou_thr=0.3, center_dist=0.0):
    kept = []
    for c in sorted(cands, key=lambda x: -x["score"]):
        box = (c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"])
        clash = False
        for k in kept:
            kbox = (k["x"], k["y"], k["x"] + k["w"], k["y"] + k["h"])
            if _iou(box, kbox) > iou_thr:
                clash = True
                break
            if center_dist and math.hypot(c["cx"] - k["cx"], c["cy"] - k["cy"]) < center_dist:
                clash = True
                break
        if not clash:
            kept.append(c)
    return kept


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def label_cell(img, text, cell_w, cell_h, band=34):
    canvas = np.full((cell_h + band, cell_w, 3), 245, np.uint8)
    ih, iw = img.shape[:2]
    scale = min(cell_w / iw, cell_h / ih)
    nr = cv2.resize(img, (max(1, int(iw * scale)), max(1, int(ih * scale))))
    oy = (cell_h - nr.shape[0]) // 2
    ox = (cell_w - nr.shape[1]) // 2
    canvas[oy:oy + nr.shape[0], ox:ox + nr.shape[1]] = nr
    cv2.rectangle(canvas, (0, 0), (cell_w - 1, cell_h + band - 1), (160, 160, 160), 1)
    cv2.putText(canvas, text, (6, cell_h + 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (20, 20, 20), 1, cv2.LINE_AA)
    return canvas


def montage(cells, cols, cell_w, cell_h):
    rows = math.ceil(len(cells) / cols)
    sheet = np.full((rows * (cell_h + 34), cols * cell_w, 3), 250, np.uint8)
    for i, (img, text) in enumerate(cells):
        r, c = divmod(i, cols)
        sheet[r * (cell_h + 34):r * (cell_h + 34) + cell_h + 34,
              c * cell_w:(c + 1) * cell_w] = label_cell(img, text, cell_w, cell_h)
    return sheet


def draw_candidates(img, cands):
    dbg = img.copy()
    for c in cands:
        # missing material gets its own colour: it is the candidate type that
        # most often turns out to be a real defect, and it should not be
        # confused with the generic blob candidates
        if c["type"] == "scratch":
            col = (0, 0, 255)
        elif c.get("subtype") == "missing_material":
            col = (0, 165, 255)
        else:
            col = (255, 0, 255)
        cv2.rectangle(dbg, (c["x"], c["y"]), (c["x"] + c["w"], c["y"] + c["h"]), col, 2)
        cv2.putText(dbg, f"C{c['index']}", (c["x"], max(16, c["y"] - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2, cv2.LINE_AA)
    return dbg


def _diverse(ccells, limit):
    """Pick the highest scoring candidates while keeping them spatially spread.

    Ranking alone lets one busy corner of the object fill the whole sheet; a
    real defect that happens to rank lower but sits elsewhere would be lost.
    """
    ordered = sorted(ccells, key=lambda t: -t[2]["score"])
    kept = []
    kept_ids = set()
    for cell in ordered:
        _, _, cand, entry = cell
        grid = entry.get("object_grid_size", [1, 1])
        radius = 0.10 * max(grid)
        cx = cand["center_in_object"][0] * grid[0]
        cy = cand["center_in_object"][1] * grid[1]
        clash = False
        for _, _, kc, ke in kept:
            kg = ke.get("object_grid_size", [1, 1])
            kx = kc["center_in_object"][0] * kg[0]
            ky = kc["center_in_object"][1] * kg[1]
            if ke is entry and math.hypot(cx - kx, cy - ky) < radius:
                clash = True
                break
        if not clash:
            kept.append(cell)
            kept_ids.add(id(cell))
        if len(kept) >= limit:
            break
    # fill the remaining slots with the best of the rest
    if len(kept) < limit:
        for cell in ordered:
            if id(cell) in kept_ids:
                continue
            kept.append(cell)
            kept_ids.add(id(cell))
            if len(kept) >= limit:
                break
    return kept


# --------------------------------------------------------------------------
# per view processing
# --------------------------------------------------------------------------

def object_grid(crop, min_side, max_side):
    h, w = crop.shape[:2]
    scale = 1.0
    if min(h, w) < min_side:
        scale = min_side / float(min(h, w))
    if max(h, w) * scale > max_side:
        scale = max_side / float(max(h, w))
    if abs(scale - 1.0) < 1e-3:
        return crop, 1.0
    interp = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
    return cv2.resize(crop, (max(1, int(w * scale)), max(1, int(h * scale))),
                      interpolation=interp), scale


def process_view(img_path, item_dir, view_name, args):
    img = cv2.imread(str(img_path))
    if img is None:
        return None
    mask, info = segment_object(img)
    if not info.get("ok"):
        return {"view": view_name, "source": str(img_path), "error": info.get("reason")}

    vdir = item_dir / view_name
    vdir.mkdir(parents=True, exist_ok=True)

    x, y, w, h = info["bbox"]
    pad = int(0.02 * max(w, h))
    x0 = max(0, x - pad); y0 = max(0, y - pad)
    x1 = min(img.shape[1], x + w + pad); y1 = min(img.shape[0], y + h + pad)
    crop = img[y0:y1, x0:x1]
    cmask = mask[y0:y1, x0:x1]

    grid, gscale = object_grid(crop, args.grid_min_side, args.grid_max_side)
    cmask = cv2.resize(cmask, (grid.shape[1], grid.shape[0]),
                       interpolation=cv2.INTER_NEAREST)
    obj_meta = save_jpg(vdir / "object.jpg", grid)

    inv = cv2.inpaint(grid, ((cmask == 0) * 255).astype(np.uint8), 7, cv2.INPAINT_TELEA)
    gray = cv2.cvtColor(inv, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    sigma = estimate_sigma(gray, cmask)
    # the material response needs the *original* colours: inpainting the
    # background away would otherwise bleed the backdrop into the surface
    lab = cv2.cvtColor(grid, cv2.COLOR_BGR2LAB)

    cands = propose(gray, cmask, sigma, lab=lab)
    cands = nms(cands, iou_thr=0.3, center_dist=max(12.0, 0.015 * max(grid.shape)))
    lines = [c for c in cands if c["type"] == "scratch"]
    blobs = [c for c in cands if c["type"] == "chip"]
    lines = sorted(lines, key=lambda c: -c["score"])[:args.max_line_cands]
    blobs = sorted(blobs, key=lambda c: -c["score"])[:args.max_blob_cands]
    cands = sorted(lines + blobs, key=lambda c: -c["score"])

    cdir = vdir / "cands"
    cdir.mkdir(exist_ok=True)
    for i, c in enumerate(cands, start=1):
        cw, ch = c["w"], c["h"]
        margin = int(max(10, 0.8 * max(cw, ch)))
        cx0 = max(0, c["x"] - margin); cy0 = max(0, c["y"] - margin)
        cx1 = min(grid.shape[1], c["x"] + cw + margin)
        cy1 = min(grid.shape[0], c["y"] + ch + margin)
        cell = _resize_up(grid[cy0:cy1, cx0:cx1], args.cand_min_side)
        cell = _resize_max(cell, args.cand_max_side)
        fname = f"cand-{i:02d}.jpg"
        save_jpg(cdir / fname, cell)
        c["crop"] = f"cands/{fname}"
        c["index"] = i
        c["bbox_in_object"] = [int(c["x"]), int(c["y"]), int(cw), int(ch)]
        c["bbox_in_source"] = [int(c["x"] * gscale + x0), int(c["y"] * gscale + y0),
                               int(cw * gscale), int(ch * gscale)]
        c["center_in_object"] = [round(c["cx"] / max(1, grid.shape[1]), 4),
                                 round(c["cy"] / max(1, grid.shape[0]), 4)]

    save_jpg(vdir / "anomaly.jpg", draw_candidates(grid, cands))
    if args.debug_overlay:
        save_jpg(vdir / "debug_overlay.jpg", draw_candidates(grid, cands))

    tiles = []
    tdir = vdir / "tiles"
    tdir.mkdir(exist_ok=True)
    ts = args.tile_size
    step = max(1, int(ts * (1 - args.tile_overlap)))
    ny = max(1, math.ceil((grid.shape[0] - step) / step) + 1)
    nx = max(1, math.ceil((grid.shape[1] - step) / step) + 1)
    while nx * ny > args.max_tiles:
        step = int(step * 1.25)
        ny = max(1, math.ceil((grid.shape[0] - step) / step) + 1)
        nx = max(1, math.ceil((grid.shape[1] - step) / step) + 1)
    for r in range(ny):
        for c in range(nx):
            tx, ty = c * step, r * step
            tx1 = min(grid.shape[1], tx + ts)
            ty1 = min(grid.shape[0], ty + ts)
            if tx1 - tx < 24 or ty1 - ty < 24:
                continue
            cell = _resize_up(grid[ty:ty1, tx:tx1], args.tile_min_side)
            cell = _resize_max(cell, args.tile_max_side)
            fn = f"tile-r{r+1}c{c+1}.jpg"
            meta = save_jpg(tdir / fn, cell)
            tiles.append({"file": f"tiles/{fn}",
                          "bbox_in_object": [tx, ty, tx1 - tx, ty1 - ty],
                          "bbox_in_source": [int(tx * gscale + x0), int(ty * gscale + y0),
                                             int((tx1 - tx) * gscale), int((ty1 - ty) * gscale)],
                          "image": meta})

    return {
        "view": view_name,
        "source": str(img_path),
        "source_size": [img.shape[1], img.shape[0]],
        "object_bbox": [x, y, w, h],
        "object_grid_size": [grid.shape[1], grid.shape[0]],
        "grid_scale": round(gscale, 4),
        "object_file": "object.jpg",
        "object_image": obj_meta,
        "anomaly_file": "anomaly.jpg",
        "noise_sigma": round(sigma, 3),
        "candidates": cands,
        "tiles": tiles,
        "segmentation": {k: info[k] for k in ("bg_rgb", "threshold", "area_frac") if k in info},
    }


def main():
    ap = argparse.ArgumentParser(description="prepare multi-view photos for defect inspection")
    ap.add_argument("--input", default="images", help="root folder containing item-*/ folders")
    ap.add_argument("--work", default="work", help="output folder")
    ap.add_argument("--items", default="", help="comma separated item ids, default all")
    ap.add_argument("--grid-min-side", type=int, default=1000)
    ap.add_argument("--grid-max-side", type=int, default=1568)
    ap.add_argument("--cand-max-side", type=int, default=512)
    ap.add_argument("--cand-min-side", type=int, default=384)
    ap.add_argument("--tile-size", type=int, default=384)
    ap.add_argument("--tile-min-side", type=int, default=1024)
    ap.add_argument("--tile-max-side", type=int, default=1024)
    ap.add_argument("--tile-overlap", type=float, default=0.25)
    ap.add_argument("--max-tiles", type=int, default=24)
    ap.add_argument("--max-line-cands", type=int, default=20)
    ap.add_argument("--max-blob-cands", type=int, default=40)
    ap.add_argument("--deep", action="store_true",
                    help="4.9x tiles instead of 2.7x (slower, better for faint defects)")
    ap.add_argument("--debug-overlay", action="store_true")
    args = ap.parse_args()
    if args.deep:
        args.tile_size = 208
        args.max_tiles = 60

    src = Path(args.input)
    if not src.is_dir():
        sys.exit(f"输入目录不存在: {src}")
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    wanted = {s.strip() for s in args.items.split(",") if s.strip()}
    item_dirs = sorted([d for d in src.iterdir() if d.is_dir() and
                         (not wanted or d.name in wanted)])
    if not item_dirs:
        sys.exit("没有找到任何 item-* 目录")

    manifest = {"input": str(src), "work": str(work),
                "grid": {"min_side": args.grid_min_side, "max_side": args.grid_max_side},
                "items": {}}
    for item_dir in item_dirs:
        views = sorted([p for p in item_dir.iterdir()
                        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp")])
        if not views:
            continue
        out_dir = work / item_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for p in views:
            res = process_view(p, out_dir, p.stem, args)
            if res:
                entries.append(res)
        if not entries:
            continue

        cells = []
        for e in entries:
            img = cv2.imread(str(out_dir / e["view"] / "object.jpg"))
            if img is not None:
                cells.append((img, e["view"]))
        if cells:
            sheet = montage(cells, cols=min(3, len(cells)), cell_w=560, cell_h=560)
            save_jpg(out_dir / "overview.jpg", sheet)

        ccells = []
        for e in entries:
            for c in e["candidates"]:
                img = cv2.imread(str(out_dir / e["view"] / c["crop"]))
                if img is None:
                    continue
                ccells.append((img, f"{e['view'].replace('view-', 'v')}c{c['index']:02d}",
                               c, e))
        if ccells:
            ccells = _diverse(ccells, 32)
            sheet = montage([(c[0], c[1]) for c in ccells],
                            cols=min(8, len(ccells)), cell_w=224, cell_h=224)
            save_jpg(out_dir / "candsheet.jpg", sheet)

        manifest["items"][item_dir.name] = {
            "dir": str(out_dir),
            "overview": "overview.jpg",
            "candsheet": "candsheet.jpg" if ccells else None,
            "views": entries,
        }
        n_c = sum(len(e.get("candidates", [])) for e in entries)
        print(f"[{item_dir.name}] views={len(entries)} candidates={n_c}")

    (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print(f"manifest -> {work / 'manifest.json'}")


if __name__ == "__main__":
    main()
