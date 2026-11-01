# surface-defect-inspect — 多角度照片表面缺陷检测 Skill

基于**通用多模态大模型**的表面细节识别 Skill。输入同一物体的多角度照片组，
识别两类表面细节，并输出可量化评价的结构化报告。

| 目标类别 | `type` | 形态 |
| --- | --- | --- |
| 线状痕迹 | `scratch` | 划痕、擦痕、擦伤、裂纹、发丝纹、拉丝 |
| 局部缺损 | `chip` | 崩缺、缺口、掉角、凹坑、材料缺失、孔洞剥落 |

## 一句话说明

经典 CV 负责**提案**（前景分割 + 候选区域，高召回），多模态模型负责**判读**
（定类型 / 严重度 / 置信度），再按 `(物体, 类型, 部位)` 做**跨视角互证**，
最终由 `merge.py` 汇总成 `findings.json / report.md / summary.csv`。

## 环境要求

* Python 3.9+
* 一个能执行本 Skill 的本地 Agent（例如 [Claude Code](https://claude.com/claude-code)）
* 依赖：`pip install opencv-python numpy`

## 三步跑起来

```bash
# 1) 安装依赖
pip install opencv-python numpy

# 2) 预处理：切前景、生成放大瓦片与候选区域（输入 images/，产出 work/）
python surface-defect-inspect/scripts/prepare.py --input images --work work

# 3) 让 Agent 执行检视，然后汇总出报告
#    在 Claude Code 里说：
#    「用 surface-defect-inspect skill 检测 images/ 下所有物体的表面划痕和崩缺」
python surface-defect-inspect/scripts/merge.py --work work --findings findings --out results
```

> 路径里的 `surface-defect-inspect/` 取决于你把本目录放在哪里，见「安装」。

## 安装（让 Agent 发现这个 Skill）

本目录就是一个完整的 Skill，复制到 Agent 能发现的位置即可：

```bash
# 方式 A：项目级（放在仓库根目录的 .claude/skills/ 下）
cp -r surface-defect-inspect <你的项目>/.claude/skills/

# 方式 B：全局（对本机所有项目生效）
cp -r surface-defect-inspect ~/.claude/skills/
```

安装后，在 Claude Code 里直接说：

```
用 surface-defect-inspect skill 检测 images/ 下所有物体的表面划痕和崩缺
```

## 输入约定

```
images/
  item-p01/view-01.png view-02.png ...   # 一个物体 = 一个目录，目录内是它的多角度照片
  item-p02/view-01.png ...
```

* 每个 `item-*` 目录 = 一个物体；目录内所有图片 = 该物体的不同视角。
* 允许 jpg/png 等常见格式；白底 / 纯色影棚背景效果最好（前景分割依赖背景均匀）。

## 检测流程（Agent 内部执行，SKILL.md 已写死规则）

1. **粗筛** —— 读 `overview.jpg` 拼版，判断哪些视角可疑
2. **逐视角主检** —— 读每个 `<view>/object.jpg`（全分辨率）与 `anomaly.jpg`（带 CV 候选框）
3. **放大取证** —— 读 `tiles/`、`cands/`，或 `zoom.py` 按归一化坐标按需取景
4. **判读分类** —— 按 `references/defect-taxonomy.md` 定类型 / 子类 / 严重度 / 置信度
5. **跨视角互证 + 汇总** —— 写 `findings/<item>.json`，跑 `merge.py`

`prepare.py` 产出（`work/` 下）：

```
work/manifest.json                  # 全量索引（位置、候选、瓦片）
work/item-p01/overview.jpg          # 该物体所有视角拼版（仅分诊用）
work/item-p01/candsheet.jpg         # 候选区域拼版（≤24 格）
work/item-p01/view-01/object.jpg    # 视角主图（物体坐标系，1000–1568px）
work/item-p01/view-01/anomaly.jpg   # 主图 + CV 候选框（红=线状，洋红=块状，橙=材料缺失）
work/item-p01/view-01/tiles/*.jpg   # 2.7x 重叠放大瓦片
work/item-p01/view-01/cands/*.jpg   # 候选区域放大裁剪
```

常用参数：`--items item-p01,item-p17`（只处理指定物体）、
`--deep`（瓦片倍率升到 4.9x，适合微弱缺陷）、`--debug-overlay`（输出标注图）。

## 输出

```bash
python surface-defect-inspect/scripts/merge.py --work work --findings findings --out results
```

| 文件 | 内容 |
| --- | --- |
| `results/findings.json` | 完整结构化结果：每物体的缺陷实例 + 逐视角观察 + 统计 |
| `results/report.md` | 人读报告（总览表 + 逐项明细表） |
| `results/summary.csv` | 扁平表，每行一个缺陷实例，量化评价可直接用 |

### 核心字段

* `verdict` ∈ `defective` / `clean` / `uncertain` —— 每个物体必须有结论，没报缺陷也要写 `clean`
* `type` ∈ `scratch` / `chip`；`subtype` 见 `references/defect-taxonomy.md`
* `severity` ∈ `minor` / `moderate` / `severe`
* `x`, `y` —— 缺陷中心在**该视角物体网格**内的归一化坐标（0–1）
* `corroborated` —— 同一缺陷在 ≥2 个视角的同一部位复现则为 `true`，自动上调置信度

完整 schema 见 `references/output-spec.md`，可直接填写的模板见
`templates/findings_template.json`。

## 量化评价（可选）

```bash
python surface-defect-inspect/scripts/evaluate.py \
  --work work --results results --gt ground_truth.json
```

输出 precision / recall / F1 / 定位误差 / 类型准确率 / 逐物体判定准确率。
真值文件格式见 `examples/ground_truth.json` 与 `scripts/evaluate.py` 的文档字符串。

## 目录

```
surface-defect-inspect/
├── SKILL.md                      # Agent 执行入口（含完整五阶段流程与规则）
├── README.md                     # 本文件，使用说明
├── references/
│   ├── defect-taxonomy.md        # 两类缺陷判定准则、混淆对照、严重度分级
│   ├── workflow.md               # 作业流程细则、升级策略、常见误判模式
│   └── output-spec.md            # findings JSON schema 与量化口径
├── templates/
│   └── findings_template.json    # 可直接复制的填写模板
├── scripts/
│   ├── prepare.py                # 前景分割 / 候选检测 / 渲染
│   ├── zoom.py                   # 按归一化坐标按需放大取景
│   ├── merge.py                  # 跨视角归并 + 报告生成
│   └── evaluate.py               # 对真值的量化评价
└── examples/
    ├── ground_truth.json         # 真值格式示例
    └── findings/                 # 模型填写的观察文件示例（clean / defective 各一）
```

## 设计要点

**为什么所有图片都是 JPEG。** 图像查看器（含 Claude Code 的 Read 工具）会按**编码后字节数**
降采样大图：一张 400KB 的 PNG 会被压到 1/2 分辨率，1.4MB 的会被压到 1/4，放大信息被静默丢弃。
本 Skill 的所有模型输入图都写成 ≤130KB 的 JPEG，实测可完整保留原始分辨率。

**两阶段检测。** 经典 CV 只做**提案**（高召回、低精度）：以表面噪声 σ 归一化的
z 分数同时检测细长结构（黑帽/白帽多尺度）与块状异常（中值背景差）。
判读由多模态模型完成，模型可以否决结构孔洞、接缝、反光等误报。

**多视角互证。** 同一物理缺陷在多个视角出现在同一部位，是判别「缺陷」与「光照伪影/结构」
最强的判据。`merge.py` 按 `(物体, 类型, region)` 归并逐视角观察，≥2 视角互证的实例
自动标记 `corroborated` 并上调置信度。
