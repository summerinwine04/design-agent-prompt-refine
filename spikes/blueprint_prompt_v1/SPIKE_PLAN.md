# Spike: 2.4.5 母图案 Blueprint Prompt 试制

**动机**：Collection 架构的核心新节点 2.4.5 是"看图 → 抽 blueprint DNA"。这是没先例的 prompt。在动 DB / UI / orchestrator 之前，先验证 LLM 能不能出稳定输出。

**判定通过的三条硬标准**：

1. **具体度** — Blueprint 的"母图案概念"必须落到可执行的元素，不能是"温暖植物元素"这种废话
2. **稳定性** — 同样输入跑 2 次，输出应有 ≥ 70% 概念一致（不要求字面完全相同，要求核心元素锚点 / 母配色关系两个 dict 里的关键 value 一致）
3. **可消费** — Blueprint 的字段结构能被下游 2.5 直接吃：`核心元素锚点.固定不变的元素` 是列表 not 段落，`母配色关系.主色_role` 有明确占比 + 色相范围

任一条失败 → prompt 迭代 v2；连续三版失败 → 架构层面重新考虑（可能需要更结构化的输入或分步骤）

---

## 快速用法

```powershell
# 1. cd 到项目根，激活 venv
cd C:\Users\zgj\Documents\Claude\Projects\summer\prompt-refine-agent
.\.venv\Scripts\Activate.ps1

# 2. 跑 spike（会调 OpenAI API，烧一点 token）
python spikes\blueprint_prompt_v1\run_spike.py

# 3. 看输出
# 会写到 spikes\blueprint_prompt_v1\outputs\run_<timestamp>.json
# 关键字段：blueprint / raw_text / tokens_in / tokens_out / elapsed_sec
```

**推荐跑法**：
- 连跑 2-3 次（用同样输入）
- 打开 outputs 对比，评估稳定性
- 如果输出好 → 报告"通过"，进 Option A（正式实施）
- 如果输出发散 → 改 prompt v2 再跑

## 输入数据说明

- `inputs/style_analysis.json` — 来自真实 succeeded run：`山系户外童装花型TOP热榜_YK250606-牛仔裤 / 20260618-222043-e8b239`
- `inputs/color_recognition.json` — 从同一 run 精简（保留 色号代码/营销色名/性别/底色/是否变色）
- `inputs/selected_pattern_refs.txt` — 用户预筛选的 1-3 张核心参考图路径（当前默认 3 张，可以改）

## 如何改 prompt 迭代

- v1 prompt 存在 `blueprint_prompt.md`
- 改完直接重跑 script，输出会写新的 `run_<timestamp>.json`
- 用 git diff 看 prompt 每版差异，评估哪些提示改动效果好

## 判定通过后

回到主对话，选 Option A（正式列 task list 实施）。
