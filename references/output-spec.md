# 输出规范

## 1. findings/<item>.json —— 模型填写的原始观察

每个物体一个文件。

```json
{
  "item": "item-p01",
  "verdict": "defective",
  "views_inspected": ["view-01", "view-02", "view-03", "view-04", "view-05"],
  "notes": "自由文本，记录判定依据、存疑之处、哪些视角未能确认。",
  "observations": [
    {
      "view": "view-01",
      "type": "scratch",
      "subtype": "abrasion",
      "severity": "moderate",
      "confidence": 0.85,
      "x": 0.63,
      "y": 0.34,
      "region": "表盘右侧",
      "description": "表盘右缘外侧有一道细长亮色划痕，长约 60px，方向近垂直。",
      "evidence": ["work/item-p01/view-01/cands/cand-03.jpg"],
      "defect_group": ""
    }
  ]
}
```

### 字段

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `item` | 是 | string | 物体 ID，与 `images/<item>/` 一致 |
| `verdict` | 是 | string | `defective` / `clean` / `uncertain` |
| `views_inspected` | 是 | string[] | 实际读过的视角名（`view-01` …） |
| `notes` | 否 | string | 判定依据与存疑说明 |
| `observations` | 是 | object[] | 逐视角观察，**没有缺陷时为空数组** |

观察字段：

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `view` | 是 | string | 该观察所属视角 |
| `type` | 是 | string | `scratch`（线状痕迹）/ `chip`（局部缺损） |
| `subtype` | 否 | string | `scratch` / `abrasion` / `scuff` / `crack` / `pit` / `chip` / `missing_material` / `hole` |
| `severity` | 是 | string | `minor` / `moderate` / `severe` |
| `confidence` | 是 | number | 0–1，你对"这是缺陷"的把握 |
| `x`, `y` | 是 | number | 缺陷中心在该视角**物体网格**内的归一化坐标，0–1 |
| `region` | 是 | string | 表面部位的自然语言描述，跨视角保持一致 |
| `description` | 是 | string | 一句话客观描述（形态、方向、尺度、颜色） |
| `evidence` | 否 | string[] | 你看过的证据文件路径 |
| `defect_group` | 否 | string | 同一物理缺陷的强制归并 ID（如 `g1`），跨视角无法用 `region` 统一时使用 |

## 2. results/findings.json —— 合并后的完整结果

```json
{
  "items": {
    "item-p01": {
      "item": "item-p01",
      "verdict": "defective",
      "views_inspected": ["view-01"],
      "notes": "...",
      "observations": [ ... ],
      "defects": [
        {
          "id": "item-p01-d01",
          "type": "scratch",
          "subtype": "abrasion",
          "severity": "moderate",
          "confidence": 0.95,
          "region": "表盘右侧",
          "views": ["view-01", "view-03"],
          "view_count": 2,
          "corroborated": true,
          "observations": [ ... ],
          "description": "..."
        }
      ]
    }
  },
  "stats": {
    "items_total": 30,
    "items_defective": 18,
    "items_clean": 11,
    "items_uncertain": 1,
    "defect_instances": 42,
    "instances_scratch": 27,
    "instances_chip": 15,
    "instances_corroborated": 12,
    "mean_confidence": 0.81
  }
}
```

`defects[]` 是**物理缺陷实例**（不是逐视角记录）：同一物体、同一类型、同一 `region`
的观察被合并为一个实例。

| 字段 | 说明 |
| --- | --- |
| `id` | `<item>-dNN` |
| `views` / `view_count` | 该缺陷在哪些视角被看到 |
| `corroborated` | `view_count >= 2`，跨视角互证成立 |
| `confidence` | 观察置信度的最大值；互证成立时 +0.1（上限 1.0） |
| `severity` | 取所有观察中最严重的一档 |

## 3. results/summary.csv

每行一个缺陷实例（无缺陷的物体也会有一行，`defect_id` 为空）：

```
item,defect_id,type,subtype,severity,confidence,region,views,view_count,corroborated,description
```

`views` 用 `|` 分隔，便于跨视角一致性统计。

## 4. results/report.md

* 「总览」：统计表，指标见上 `stats`。
* 「逐项明细」：按物体分节，每节一个缺陷实例表
  （缺陷ID / 类型 / 子类 / 严重度 / 置信度 / 区域 / 视角数 / 互证 / 描述）。

## 5. 量化评价常用口径

| 指标 | 计算 |
| --- | --- |
| 检出率 (recall) | 被报告的缺陷实例数 / 真值缺陷数 |
| 误报数 (FP) | `defects[]` 中真值中不存在的实例数 |
| 类型准确率 | `type` 与真值一致的比例（scratch / chip 二分类） |
| 定位误差 | 同一缺陷在同视角下 `x,y` 与真值中心的归一化距离 |
| 逐物体判定准确率 | `verdict` 与真值是否一致 |
| 互证率 | `corroborated == true` 的实例占比（反映结论稳健性） |
