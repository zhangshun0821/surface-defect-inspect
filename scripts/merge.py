#!/usr/bin/env python3
"""Merge per-view observations into a consolidated defect report.

The model inspects each view independently and writes one JSON file per item
(see references/output-spec.md for the schema).  This script

  * validates the observations against the item inventory of manifest.json
  * groups observations that describe the same physical defect
    (same item + type + surface region) into *defect instances*
  * marks instances seen in more than one view as corroborated and lifts
    their confidence accordingly
  * writes findings.json / report.md / summary.csv

Usage
-----
    python merge.py --work work --findings findings --out results
    python merge.py --work work --findings findings/item-p01.json --out results
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

TYPES = ("scratch", "chip")
SEVERITIES = ("minor", "moderate", "severe")
VERDICTS = ("defective", "clean", "uncertain")


def load_observations(paths, work):
    work = Path(work)
    manifest_p = work / "manifest.json"
    inventory = {}
    if manifest_p.exists():
        man = json.loads(manifest_p.read_text(encoding="utf-8"))
        for name, it in man["items"].items():
            inventory[name] = {"views": [v["view"] for v in it["views"]],
                               "dir": it["dir"]}

    items = {}
    for p in paths:
        p = Path(p)
        files = sorted(p.glob("*.json")) if p.is_dir() else [p]
        for f in files:
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[warn] 无法解析 {f}: {e}", file=sys.stderr)
                continue
            docs = doc if isinstance(doc, list) else [doc]
            for d in docs:
                item_id = d.get("item")
                if not item_id:
                    print(f"[warn] {f} 缺少 item 字段", file=sys.stderr)
                    continue
                entry = items.setdefault(item_id, {
                    "item": item_id,
                    "verdict": d.get("verdict", "uncertain"),
                    "views_inspected": d.get("views_inspected", []),
                    "notes": d.get("notes", ""),
                    "observations": [],
                })
                if d.get("notes"):
                    entry["notes"] = d["notes"]
                for obs in d.get("observations", []):
                    entry["observations"].append(obs)
    return items, inventory


def normalise(items, inventory):
    warnings = []
    for item_id, entry in items.items():
        if inventory and item_id not in inventory:
            warnings.append(f"{item_id}: 不在 manifest 中")
        views = inventory.get(item_id, {}).get("views", [])
        clean_obs = []
        for obs in entry["observations"]:
            t = str(obs.get("type", "")).lower()
            if t not in TYPES:
                warnings.append(f"{item_id}: 未知缺陷类型 {t!r}，已忽略")
                continue
            v = obs.get("view")
            if views and v not in views:
                warnings.append(f"{item_id}: 未知视图 {v!r}")
            sev = str(obs.get("severity", "minor")).lower()
            if sev not in SEVERITIES:
                sev = "minor"
            try:
                conf = float(obs.get("confidence", 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            conf = max(0.0, min(1.0, conf))
            clean_obs.append({
                "view": v,
                "type": t,
                "subtype": obs.get("subtype", ""),
                "severity": sev,
                "confidence": round(conf, 3),
                "x": obs.get("x"),
                "y": obs.get("y"),
                "region": (obs.get("region") or "").strip(),
                "description": (obs.get("description") or "").strip(),
                "evidence": obs.get("evidence", []),
                "defect_group": obs.get("defect_group", ""),
            })
        entry["observations"] = clean_obs
        if entry["verdict"] not in VERDICTS:
            entry["verdict"] = "uncertain"
    return warnings


def group_instances(entry):
    """Group observations of one item into physical defect instances.

    Observations are merged only across *different* views: one view cannot
    legitimately report the same physical defect twice, so two observations
    from the same view are always separate defects even if they share a
    region label.
    """
    groups = []
    for obs in entry["observations"]:
        if obs["defect_group"]:
            key = ("g", obs["defect_group"])
        else:
            key = (obs["type"], obs["region"].lower())
        for g in groups:
            if g["key"] == key and obs["view"] not in g["views"]:
                g["obs"].append(obs)
                g["views"].add(obs["view"])
                break
        else:
            groups.append({"key": key, "obs": [obs], "views": {obs["view"]}})

    instances = []
    for i, g in enumerate(sorted(groups,
                                key=lambda kv: -max(o["confidence"] for o in kv["obs"])), 1):
        obs_list = g["obs"]
        views = sorted({o["view"] for o in obs_list if o["view"]})
        conf = max(o["confidence"] for o in obs_list)
        corroborated = len(views) >= 2
        if corroborated:
            conf = min(1.0, conf + 0.1)
        inst = {
            "id": f"{entry['item']}-d{i:02d}",
            "type": obs_list[0]["type"],
            "subtype": obs_list[0]["subtype"] or "unspecified",
            "severity": _worst_severity([o["severity"] for o in obs_list]),
            "confidence": round(conf, 3),
            "region": obs_list[0]["region"] or "unspecified",
            "views": views,
            "view_count": len(views),
            "corroborated": corroborated,
            "observations": [
                {k: o[k] for k in ("view", "x", "y", "confidence", "description", "evidence")}
                for o in obs_list
            ],
            "description": next((o["description"] for o in obs_list if o["description"]), ""),
        }
        instances.append(inst)
    return instances


def _worst_severity(sevs):
    order = {"minor": 0, "moderate": 1, "severe": 2}
    return max(sevs, key=lambda s: order.get(s, 0))


def build_report(items):
    out = {"items": {}, "stats": {}}
    n_items = len(items)
    n_def = n_clean = n_unc = 0
    n_inst = n_scr = n_chip = 0
    n_corr = 0
    confs = []
    for item_id in sorted(items):
        entry = items[item_id]
        inst = group_instances(entry)
        entry["defects"] = inst
        out["items"][item_id] = entry
        if entry["verdict"] == "defective":
            n_def += 1
        elif entry["verdict"] == "clean":
            n_clean += 1
        else:
            n_unc += 1
        n_inst += len(inst)
        n_scr += sum(1 for d in inst if d["type"] == "scratch")
        n_chip += sum(1 for d in inst if d["type"] == "chip")
        n_corr += sum(1 for d in inst if d["corroborated"])
        confs += [d["confidence"] for d in inst]
    out["stats"] = {
        "items_total": n_items,
        "items_defective": n_def,
        "items_clean": n_clean,
        "items_uncertain": n_unc,
        "defect_instances": n_inst,
        "instances_scratch": n_scr,
        "instances_chip": n_chip,
        "instances_corroborated": n_corr,
        "mean_confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
    }
    return out


def write_markdown(report, path):
    s = report["stats"]
    lines = ["# 表面缺陷检测报告", "",
             "## 总览", "",
             "| 指标 | 数值 |", "| --- | --- |",
             f"| 检测物体数 | {s['items_total']} |",
             f"| 判定有缺陷 | {s['items_defective']} |",
             f"| 判定无缺陷 | {s['items_clean']} |",
             f"| 判定不确定 | {s['items_uncertain']} |",
             f"| 缺陷实例总数 | {s['defect_instances']} |",
             f"| 线状痕迹实例 | {s['instances_scratch']} |",
             f"| 局部缺损实例 | {s['instances_chip']} |",
             f"| 跨视角互证实例 | {s['instances_corroborated']} |",
             f"| 平均置信度 | {s['mean_confidence']} |", ""]

    lines += ["## 逐项明细", ""]
    for item_id in sorted(report["items"]):
        e = report["items"][item_id]
        lines.append(f"### {item_id} — {e['verdict']}")
        lines.append("")
        if e.get("views_inspected"):
            lines.append(f"- 已检视角: {', '.join(e['views_inspected'])}")
        if e.get("notes"):
            lines.append(f"- 备注: {e['notes']}")
        if not e["defects"]:
            lines.append("- 未发现缺陷")
            lines.append("")
            continue
        lines += ["", "| 缺陷ID | 类型 | 子类 | 严重度 | 置信度 | 区域 | 视角数 | 互证 | 描述 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for d in e["defects"]:
            lines.append(
                f"| {d['id']} | {'线状痕迹' if d['type'] == 'scratch' else '局部缺损'} "
                f"| {d['subtype']} | {d['severity']} | {d['confidence']:.2f} "
                f"| {d['region']} | {d['view_count']} "
                f"| {'是' if d['corroborated'] else '否'} | {d['description']} |")
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def write_csv(report, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["item", "defect_id", "type", "subtype", "severity", "confidence",
                    "region", "views", "view_count", "corroborated", "description"])
        for item_id in sorted(report["items"]):
            e = report["items"][item_id]
            if not e["defects"]:
                w.writerow([item_id, "", "", "", "", "", "", "", "", "",
                            e["verdict"] + ": no defect reported"])
                continue
            for d in e["defects"]:
                w.writerow([item_id, d["id"], d["type"], d["subtype"], d["severity"],
                            d["confidence"], d["region"], "|".join(d["views"]),
                            d["view_count"], int(d["corroborated"]), d["description"]])


def main():
    ap = argparse.ArgumentParser(description="merge per-view observations into a report")
    ap.add_argument("--work", default="work")
    ap.add_argument("--findings", required=True, help="json file or directory of json files")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    paths = [Path(args.findings)]
    items, inventory = load_observations(paths, args.work)
    warnings = normalise(items, inventory)
    report = build_report(items)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "findings.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    write_markdown(report, out / "report.md")
    write_csv(report, out / "summary.csv")

    for wmsg in warnings:
        print(f"[warn] {wmsg}", file=sys.stderr)
    print(json.dumps(report["stats"], ensure_ascii=False, indent=2))
    print(f"-> {out/'findings.json'} / {out/'report.md'} / {out/'summary.csv'}")


if __name__ == "__main__":
    main()
