# 作业流程细则

## 0. 准备

```bash
python .claude/skills/surface-defect-inspect/scripts/prepare.py --input images --work work
```

跑完后先确认产物规模：

```bash
python -c "import json;m=json.load(open('work/manifest.json',encoding='utf-8'));\
print(len(m['items']),'items');\
[print(k, len(v['views']), 'views', sum(len(x['candidates']) for x in v['views']), 'cands') \
 for k,v in m['items'].items()]"
```

## 1. 逐物体、逐视角检视

推荐对每个 `item-*` 按下面的顺序读图（每步都是一次图片读取）：

| 顺序 | 文件 | 回答的问题 |
| --- | --- | --- |
| 1 | `work/<item>/overview.jpg` | 这组照片是什么物体？哪些视角可疑？ |
| 2 | `work/<item>/<view>/object.jpg`（逐视角） | 这个视角的表面有没有线状痕迹/局部缺损？ |
| 3 | `work/<item>/<view>/anomaly.jpg` | CV 提示的位置有没有我漏掉的异常？ |

**逐视角必读 `object.jpg`。** 拼版每格只有 500px 左右，细划痕会被缩没；
`object.jpg` 是 1000–1568px 的物体网格，是判读的主证据。

### 记录什么

对每一个你认为是缺陷的部位，记一条观察：

```json
{
  "view": "view-02",
  "type": "scratch",
  "subtype": "abrasion",
  "severity": "minor",
  "confidence": 0.72,
  "x": 0.63, "y": 0.34,
  "region": "键帽右半部",
  "description": "3 道平行浅划痕，长约 40px，方向近垂直，边界弥散"
}
```

`x`/`y` 是缺陷中心在**该视角物体网格**（即 `object.jpg` 的尺寸）内的归一化坐标。
目测时用比例估算即可，误差 ±0.03 不影响评价。

## 2. 升级策略（按需，不必全做）

| 触发条件 | 动作 |
| --- | --- |
| `object.jpg` 上看到可疑但不确定的痕迹 | 读对应 `tiles/tile-rXcY.jpg`（2.7x） |
| 缺陷很小 / 疑似点蚀 | 用 `zoom.py --span 0.10` 取更小的窗口 |
| 想确认是不是反光 | 读**另一个视角**的同一部位（`zoom.py --views view-02,view-03`） |
| CV 候选框指向可疑处 | 读 `cands/cand-NN.jpg` |
| 物体很小、整图看不清 | `object.jpg` 已自动放大到 ≥1000px；仍不够就 `zoom.py --span 0.3` |
| 缺陷极微弱（浅色件上的浅划痕） | 重跑 `prepare.py --deep`，瓦片倍率升到 4.9x |

### 关于瓦片编号

`work/<item>/<view>/tiles/tile-rXcY.jpg` 中 `r` 是行、`c` 是列，从 1 开始，
按物体网格的行列顺序切分。每个瓦片在 `manifest.json` 里的 `bbox_in_object`
给出了它在物体网格中的像素范围，可用于反推哪个瓦片覆盖某个 `x, y`。

## 3. 跨视角互证

同一物理缺陷在不同视角下**位置会变**（物体转了），所以不要用坐标去匹配。
用 `region` 措辞：

* 好：所有视角都写 `"键帽右半部"`
* 差：视角 1 写 `"右侧"`，视角 2 写 `"中偏右"`

`merge.py` 按 `(物体, 类型, region)` 分组。若你确认是同一个缺陷但 `region` 描述
实在无法统一，可以给观察加 `"defect_group": "g1"` 强制归并。

**互证要点**：
* 缺陷在 ≥2 个视角的同一部位出现 → 置信度可上调到 0.9 附近。
* 只在 1 个视角出现 → 说明可能只在某个角度被光照显出来，`confidence` 控制在 0.5–0.7，
  并在 `notes` 里写明"仅在 view-XX 可见"。
* **若一个物体某个视角明显没看到任何缺陷，而其它视角有**，要在 `notes` 里点出来，
  这通常是物体本身缺陷，只在特定角度显现。

## 4. 写 findings

每个物体一个文件：`findings/<item>.json`

```json
{
  "item": "item-p17",
  "verdict": "defective",
  "views_inspected": ["view-01", "view-02", "view-03", "view-04", "view-05"],
  "notes": "view-01 键帽右半部可见多道细划痕，其余视角未见同位置异常。",
  "observations": [
    {
      "view": "view-01", "type": "scratch", "subtype": "abrasion",
      "severity": "moderate", "confidence": 0.88,
      "x": 0.63, "y": 0.34, "region": "键帽右半部",
      "description": "3 道平行浅划痕",
      "evidence": ["work/item-p17/view-01/cands/cand-01.jpg"]
    }
  ]
}
```

`evidence` 填你实际看过的证据文件路径，便于复核。

**必须包含没有缺陷的物体**：

```json
{
  "item": "item-p10",
  "verdict": "clean",
  "views_inspected": ["view-01", "view-02"],
  "notes": "两视角表面均匀，未见线状痕迹或局部缺损。",
  "observations": []
}
```

## 5. 汇总

```bash
python .claude/skills/surface-defect-inspect/scripts/merge.py --work work --findings findings --out results
```

* `results/findings.json` —— 机器可读全量结果（含 `stats`）
* `results/report.md` —— 人读报告
* `results/summary.csv` —— 扁平表，量化评价可直接用

`merge.py` 会打印统计与 schema 警告（未知类型、未知视图等），**务必读一遍**。

## 6. 自检清单

汇报前逐条核对：

- [ ] 每个 `item-*` 都有 findings 文件，`verdict` 非空
- [ ] `views_inspected` 与实际读过的视角一致
- [ ] 所有观察的 `type` 只有 `scratch` / `chip` 两个值
- [ ] `x`, `y` 在 0–1 之间
- [ ] `region` 措辞跨视角可对应
- [ ] 报告里区分了"高置信度缺陷"与"低置信度可疑项"
- [ ] `merge.py` 无 schema 警告

## 常见误判模式

| 误判 | 纠正 |
| --- | --- |
| 把产品孔洞当崩缺 | 看孔缘是否破损；规则完整的孔是结构 |
| 把渲染噪点当缺陷 | 噪点不连续、不成线、不跨视角复现 |
| 把倒角高光当划痕 | 高光沿轮廓走，随视角移动 |
| 把 logo/铭文当划痕 | 文字/图形有设计意图，边缘规则 |
| 一条划痕报成 5 条 | 同一视角内的密集平行线合并成一条 |
| 只看了拼版就下结论 | 必须看 `object.jpg` / 瓦片 |
| 把 JSON 存成 PNG | 一律 JPEG；大 PNG 会被降采样 |
