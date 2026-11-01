---
name: surface-defect-inspect
description: >
  多模态表面缺陷检测。对同一物体的多角度照片组（item-*/view-*.png），识别两类表面细节：
  线状痕迹（划痕、擦痕、裂纹）与局部缺损（崩缺、凹坑、材料缺失），输出带定位、类型、严重度、
  置信度与跨视角互证的结构化报告（findings.json / report.md / summary.csv）。
  Use this skill whenever the user asks to 检测表面缺陷 / 找划痕 / 检测崩缺 / 外观质检 /
  缺陷识别 / 多角度照片异常检测, defect inspection, scratch detection, chip detection,
  surface anomaly detection. Triggers: "表面缺陷检测","找划痕","检测崩缺","外观质检",
  "缺陷识别","瑕疵检测","多角度照片","defect inspection","scratch","chip","瑕疵".
---

# 表面缺陷检测（多角度照片）

## 目标

输入若干组物体的多角度照片，每组照片属于同一个物体；输出每一组的表面缺陷清单，
覆盖两类细节：

| 类别 | type | 典型形态 |
| --- | --- | --- |
| 线状痕迹 | `scratch` | 划痕、擦痕、划伤、裂纹、发丝纹、摩擦痕 |
| 局部缺损 | `chip` | 崩缺、缺口、掉角、凹坑、材料缺失、孔洞、剥落 |

判定准则、两类混淆对照与严重度分级见 `references/defect-taxonomy.md`——**判读前必须先读它**。

## 输入约定

```
images/
  item-p01/view-01.png ... view-05.png     # 同一物体的多角度照片
  item-p02/view-01.png ...
```
* 每个 `item-*` 目录 = 一个物体；目录内所有图片 = 该物体的不同视角。
* 允许 jpg/png 等常见格式；白底/纯色影棚背景效果最好（前景分割依赖背景均匀）。

## 环境依赖

```bash
pip install opencv-python numpy
```

## 一键准备

```bash
python .claude/skills/surface-defect-inspect/scripts/prepare.py --input images --work work
```

可选参数：

| 参数 | 说明 |
| --- | --- |
| `--items item-p01,item-p17` | 只处理指定物体 |
| `--deep` | 瓦片放大倍率 2.7x → 4.9x，更慢，适合微弱缺陷 |
| `--debug-overlay` | 额外输出标注框图 |
| `--grid-max-side 1568` | 单图最长边上限（默认 1568，再大只会被下游缩放） |

产出（每个 `work/<item>/` 下）：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `overview.jpg` | 该物体所有视角的拼版 | 阶段 1 粗筛 |
| `<view>/object.jpg` | 该视角的前景裁剪（物体坐标系） | 阶段 1 主检 |
| `<view>/anomaly.jpg` | 同上 + CV 候选框 C01/C02… | 阶段 1/2 辅助定位 |
| `<view>/tiles/tile-rXcY.jpg` | 重叠放大瓦片（2.7x） | 阶段 2 细查 |
| `<view>/cands/cand-NN.jpg` | 候选区域放大裁剪 | 阶段 2 分类取证 |
| `candsheet.jpg` | 该物体全部候选的拼版（≤24 格） | 阶段 2 批量核验 |
| `../manifest.json` | 上述全部的机器可读索引 | 定位与复现 |

## 检测流程

### 阶段 1 · 逐物体粗筛（必做，成本最低）

对每个 `item-*`：

1. 读 `work/<item>/overview.jpg`：一眼看完所有视角，判断该物体**是否值得细查**、哪些视角可疑。
2. 对**每一个视角**读 `work/<item>/<view>/object.jpg`（这是全分辨率主图，务必逐视角读，
   不要只看拼版——拼版缩放后会丢掉细划痕）。
3. 读 `work/<item>/<view>/anomaly.jpg`（与上一步同一视角）：CV 候选框只作**提示**，
   框住的地方可能是几何结构（孔洞、接缝、倒角），必须自己看图判断，**不要照抄框的类型**。

> 判读要求：只要看到"不属于产品设计结构"的线状或块状痕迹，就记一条观察，不要因为"不确定"
> 而略过；不确定性用 `confidence` 表达，而不是靠不报。

### 阶段 2 · 放大取证（可疑处必须做）

* CV 候选：读 `work/<item>/<view>/cands/cand-NN.jpg`，或在 `candsheet.jpg` 里批量看。
* CV 没框到但你看着可疑：读该区域对应的 `tiles/tile-rXcY.jpg`（瓦片编号与
  `manifest.json` 里 `bbox_in_object` 对应）。
* 任意区域按需放大：

```bash
python .claude/skills/surface-defect-inspect/scripts/zoom.py --work work \
  --item item-p17 --view view-01 --x 0.62 --y 0.35 --span 0.18 --out _t/zoom.jpg
```
  `--x/--y` 是物体网格内的归一化中心坐标，`--span` 是窗口宽度占物体网格长边的比例。
  `--views view-02,view-03` 可同时取其它视角的同一归一化区域。

### 阶段 3 · 判读与分类

按 `references/defect-taxonomy.md` 判定类型/子类/严重度，并给出：
* `x`, `y`：缺陷中心在**该视角物体网格**内的归一化坐标（0–1）
* `region`：缺陷所在表面的自然语言部位描述（如"键帽右半部""表盘右侧"），跨视角合并要用
* `description`：一句话客观描述
* `confidence`：0–1

### 阶段 4 · 跨视角互证（决定漏报/误报的关键）

同一物理缺陷会在多个视角出现。**逐视角独立记录观察**，然后在 `region` 上用一致的措辞，
`merge.py` 会把同物体 + 同类型 + 同 `region` 的观察合并成一个缺陷实例；
在 ≥2 个视角出现的实例会被标记 `corroborated` 并自动上调置信度。

若某个观察在其它视角**找不到对应**，要么是真实的表面细节，要么该视角根本没看到它——
把这种情况写进 `notes`，并降低该观察的 `confidence`。

### 阶段 5 · 汇总

把每个物体的观察写成 `findings/<item>.json`（格式见 `references/output-spec.md`，
模板见 `templates/findings_template.json`），然后：

```bash
python .claude/skills/surface-defect-inspect/scripts/merge.py \
  --work work --findings findings --out results
```

产出：

* `results/findings.json` —— 完整结构化结果（含每物体的缺陷实例、逐视角观察、统计）
* `results/report.md` —— 人读报告（总览表 + 逐项明细表）
* `results/summary.csv` —— 量化评价用的扁平表（每行一个缺陷实例）

## 输出要求（务必遵守）

* 每个物体都要有结论：`verdict` ∈ `defective` / `clean` / `uncertain`。
  "没报缺陷"必须写 `clean`（并列出已检视角），不能省略该物体——漏报的代价最高。
* `views_inspected` 必须列全实际看过的视角。
* 报告语言用中文；类型、严重度等枚举值用英文固定串，便于量化统计。
* 缺陷位置一律用**物体网格归一化坐标**，不要用像素坐标。

## 已知坑（务必规避）

1. **图片字节预算**：下游图像查看器会按**编码后字节数**降采样大图（PNG 尤其容易被压到
   1/2～1/4 分辨率），放大信息会被静默丢弃。本 skill 的所有图片都用 ≤130KB 的 JPEG 输出，
   **不要自行转成 PNG**，也不要把大图塞给模型。
2. **拼版不是主图**：`overview.jpg` 是缩略拼版，只能用来分诊；判定必须基于
   `object.jpg` / `tiles` / `cands`。
3. **CV 候选会误报**：几何结构（螺丝孔、接缝、倒角、按键间隙、反光）都会触发候选框。
   候选框是"值得看一眼"的提示，不是缺陷结论。
4. **反光 ≠ 划痕**：高光带、镜面反射的边界是正常现象；线状痕迹在**多个视角**下位置随
   表面起伏一致变化，而反光随视角改变。
5. **同一处只报一次**：一个物理缺陷在 5 个视角出现就应该是 1 条观察 × 5 个视角，
   不是 5 条独立缺陷。用一致的 `region` 措辞让 `merge.py` 正确合并。
6. **候选框的颜色**：`anomaly.jpg` 上**红色=线状痕迹**，**洋红=通用块状**，
   **橙色=材料缺失（`missing_material`）**。橙色候选表示"这块区域颜色/质感与母材不同，
   可能是露出底材"，请**逐个看图判读**，不要因为它位于两种材料的交界处就当成结构开口放过
   （判据见 `references/defect-taxonomy.md` 的"材料缺失 vs 材料交界"）。
7. **白色/浅色零件要特别小心**：浅色件（如白色泡沫套管）与白色背景对比极低，
   前景分割容易整片丢失。`object.jpg` 里若某块材料看起来"被裁掉"或边界怪异，
   那是真实缺损的可能信号，务必放大确认。

## 命令速查

```bash
# 准备
python .claude/skills/surface-defect-inspect/scripts/prepare.py --input images --work work

# 任意区域放大
python .claude/skills/surface-defect-inspect/scripts/zoom.py --work work \
  --item item-p01 --view view-01 --x 0.5 --y 0.5 --span 0.15 --out _t/z.jpg

# 汇总并出报告
python .claude/skills/surface-defect-inspect/scripts/merge.py \
  --work work --findings findings --out results
```

## 参考文件

* `references/defect-taxonomy.md` —— 两类缺陷的判定准则、混淆对照、严重度分级
* `references/workflow.md` —— 详细作业流程、升级策略、提示词模板、常见误判模式
* `references/output-spec.md` —— findings JSON schema 与报告字段说明
* `templates/findings_template.json` —— 可直接复制的填写模板
