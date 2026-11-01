#!/usr/bin/env python3
"""Quantitative evaluation of the inspection report against a ground truth file.

Ground truth format (JSON, UTF-8)::

    {
      "items": {
        "item-p01": {
          "views": ["view-01", "view-02"],
          "defects": [
            {
              "id": "g1",
              "type": "scratch",
              "view": "view-01",
              "x": 0.62, "y": 0.34,
              "bbox": [0.60, 0.32, 0.05, 0.02],
              "region": "表盘右侧"
            }
          ]
        }
      }
    }

`x`, `y` are normalised coordinates in the object grid of the given view
(identical convention to the findings).  `bbox` is optional and, when given,
is `[x, y, w, h]` also normalised.

A predicted defect instance matches a ground truth defect when

  * the types are equal, and
  * the instance was observed in the ground truth's view (if the instance was
    seen in several views any of them may match), and
  * either the normalised centre distance is within `--tol` or the region
    strings agree.

Usage::

    python evaluate.py --work work --results results --gt ground_truth.json
    python evaluate.py --work work --results results --gt gt.json --tol 0.05
"""

import argparse
import json
import math
import sys
from pathlib import Path


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def region_key(s):
    return "".join(ch for ch in (s or "").lower() if ch.strip())


def dist(a, b):
    if a is None or b is None:
        return None
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def match(pred_inst, gt, tol, strict_view=True):
    if pred_inst["type"] != gt["type"]:
        return None
    obs = pred_inst.get("observations", [])
    gt_view = gt.get("view")
    if strict_view and gt_view:
        same_view = [o for o in obs if o.get("view") == gt_view]
        if not same_view:
            return None
        pool = same_view
    else:
        pool = obs
    if not pool:
        return None
    best = None
    for o in pool:
        d = dist((o.get("x"), o.get("y")), (gt.get("x"), gt.get("y")))
        if d is None:
            continue
        if best is None or d < best[0]:
            best = (d, o)
    if best is None:
        if region_key(pred_inst.get("region")) and \
                region_key(pred_inst.get("region")) == region_key(gt.get("region")):
            return 0.0
        return None
    d = best[0]
    if d > tol:
        if region_key(pred_inst.get("region")) and \
                region_key(pred_inst.get("region")) == region_key(gt.get("region")):
            return d
        return None
    return d


def main():
    ap = argparse.ArgumentParser(description="evaluate the report against ground truth")
    ap.add_argument("--work", default="work")
    ap.add_argument("--results", default="results", help="directory written by merge.py")
    ap.add_argument("--gt", required=True)
    ap.add_argument("--tol", type=float, default=0.05,
                    help="normalised centre distance tolerance")
    ap.add_argument("--min-confidence", type=float, default=0.0,
                    help="ignore predicted instances below this confidence")
    args = ap.parse_args()

    results = load_json(Path(args.results) / "findings.json")
    gt = load_json(args.gt)
    gt_items = gt.get("items", gt)

    tp = fp = fn = 0
    loc_errors = []
    type_hits = {"scratch": [0, 0], "chip": [0, 0]}
    per_item = []
    matched_gt = set()

    for item_id, gt_item in gt_items.items():
        gts = gt_item.get("defects", []) if isinstance(gt_item, dict) else gt_item
        preds = results.get("items", {}).get(item_id, {}).get("defects", [])
        preds = [p for p in preds if p.get("confidence", 0) >= args.min_confidence]
        used = set()
        for gt_def in gts:
            gt_key = (item_id, gt_def.get("id", id(gt_def)))
            hit = None
            for pi, p in enumerate(preds):
                if pi in used:
                    continue
                d = match(p, gt_def, args.tol)
                if d is not None:
                    hit = (pi, d)
                    break
            if hit:
                used.add(hit[0])
                tp += 1
                matched_gt.add(gt_key)
                loc_errors.append(hit[1])
                p = preds[hit[0]]
                type_hits[p["type"]][0] += 1
                type_hits[p["type"]][1] += 1
            else:
                fn += 1
                # a wrong-typed prediction near the defect counts as a type error
                for p in preds:
                    if p["type"] != gt_def["type"] and p not in [preds[i] for i in used]:
                        d = dist((p.get("observations") or [{}])[0].get("x"),
                                 (p.get("observations") or [{}])[0].get("y"))
                        if d is not None and d <= args.tol * 2:
                            type_hits[gt_def["type"]][1] += 1
                            break
        fp += len(preds) - len(used)
        per_item.append({
            "item": item_id,
            "gt": len(gts),
            "tp": len(gts) - sum(1 for g in gts
                                 if (item_id, g.get("id", id(g))) not in matched_gt),
            "pred": len(preds),
            "fp": len(preds) - len(used),
        })

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # per item verdict accuracy
    verdict_ok = verdict_total = 0
    for item_id, gt_item in gt_items.items():
        gts = gt_item.get("defects", []) if isinstance(gt_item, dict) else gt_item
        want = "defective" if gts else "clean"
        got = results.get("items", {}).get(item_id, {}).get("verdict")
        verdict_total += 1
        verdict_ok += int(got == want)

    metrics = {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "mean_loc_error": round(sum(loc_errors) / len(loc_errors), 5) if loc_errors else None,
        "max_loc_error": round(max(loc_errors), 5) if loc_errors else None,
        "type_accuracy": {
            t: round(h / n, 4) if n else None for t, (h, n) in type_hits.items()
        },
        "verdict_accuracy": round(verdict_ok / verdict_total, 4) if verdict_total else None,
        "items_evaluated": verdict_total,
        "tolerance": args.tol,
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    out = Path(args.results) / "evaluation.json"
    out.write_text(json.dumps({"metrics": metrics, "per_item": per_item},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
