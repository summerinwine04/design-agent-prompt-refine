# 拍摄中心与选片 产品方案 PRD

- 版本：v1.1（2026-07-26；新增拍摄工作台 + 导航更名）
- 状态：已确认方向，待评审细节
- 位置：prompt-refine-agent 新增「🎬 拍摄工作台」与「📸 拍摄中心」两个导航 tab（与设计生图链路明确分开）；原「工作台」更名为「设计方案工作台」
- 上游：Fitting Room（look 组套 + 拍摄槽位）；下游：商品可用图组回流 + 详情页长图 + 运营交付包
- 参考实现：ai-supply/shooting（三步流水线 + naturalize 后处理，本 PRD 将其框架并入本仓后端）

---

## 1. 问题陈述

Fitting Room 完成 look 组套后，拍摄环节目前完全离线：手工把 look 的上/下装图和场景图拼成 shooting 的 batch_task 文件夹 + CSV，命令行跑 run_tasks.py，再翻 HTML 报告人工挑图。数据断链（look ↔ 拍摄产物无关联）、门控缺失（首图废了套图照烧，k=6 时浪费 6 倍生图成本）、选片结果不回流（运营拿不到"这个商品最终可用哪几张图"的结构化结论），详情页长图还要再开 PS 手工拼。

而 shoot CSV 所需字段（上/下装参考图、缺侧文本表达、场景参考图）与 Fitting Room 的 look 数据一一对应——缺的只是执行层和两个界面。

## 2. 目标

1. look → 拍摄任务零手工：从 Fitting Room 勾选 look 或按波段发起拍摄批次，无需接触文件夹/CSV。
2. 两段式成本门控：首图确认后才延展套图，避免废首图连带浪费套图生图费。
3. 选片结果结构化回流：每个 look 有一组"拍摄可用图"（含用途标记），系统内可查、运营可一键取走。
4. 详情页长图一键生成：按选片排序自动拼接输出，不再手工 PS。

## 3. 非目标

- **不做多场景候选**（同 look 多版首图选一）——v1 每个 look 用拍摄槽位那一张场景图，槽位可换后重拍（Q4 已定，未来做）。
- **不做模特库**——模特身份沿用 shoot 现有兜底逻辑：无独立模特图时从场景参考图主位人物提取。
- **不动设计生图链路**——拍摄中心是独立 tab、独立 router、独立表；与工作台/生图任务仅通过 look 数据衔接。
- **不做 AI 自动选片/打分**——v1 纯人工选片（P2 可加辅助排序）。
- **不承诺"不被判定为 AI"**——naturalize 沿用 shooting 现有能力与免责边界。

## 4. 用户故事

- 作为运营，我在 Fitting Room 把某波段的 look 组好后，点「📸 发起拍摄」选中 N 套 look 建一个拍摄批次，系统自动带上每套的上/下装图、缺侧文本和拍摄槽位场景图。
- 作为运营，我在拍摄中心看到每套 look 的首图陆续出来，满意的点「确认，延展套图」，不满意的换场景图/补一句指令点「重拍首图」。
- 作为运营，套图出齐后我进选片模式：每张图 keep/reject，给 keep 的图标用途（主图/详情页/小红书）并拖拽排序。
- 作为运营，选片完成后我一键生成该 look 的详情页长图，并下载包含全部选中图（已 naturalize）+ 长图 + 离线预览页的交付包。
- 作为运营，之后任何时候我都能在系统里按 look/波段找到"这个商品的可用图组"。

## 4.5 信息架构：双层结构与导航调整（2026-07-26 新增决策）

对齐设计链路已验证的「工作台调 prompt / 任务量产」双层结构：

| 层 | 设计链路（现有） | 拍摄链路（本 PRD） |
|---|---|---|
| prompt 迭代实验 | 设计方案工作台（原「工作台」，更名） | **🎬 拍摄工作台**（新增） |
| 批量生产 | 设计生图任务 | **📸 拍摄中心**（新增） |

**拍摄工作台**（shooting prompt 的独立调试环境）：

- [ ] 输入面板：单 look 级输入——上/下装参考图（可从选款中心选、可临时上传）、缺侧文本表达、场景参考图（模板库选/上传）、模特参考图（可选）、套图 k。
- [ ] 单次实验运行：跑 首图链路（step2 推理 → 生图）或 套图链路（step3.1 → 3.2），节点级展示中间产物——两段 JSON 推理结果（主体锁定/场景解析/镜头企划）与成图，对齐设计方案工作台的 trace 查看体验。
- [ ] shooting 三个 prompt 框架（场景图/套图延展/参考图美化）纳入现有「Prompt 版本」管理，工作台内可选版本跑 A/B。
- [ ] 实验产物与拍摄中心量产数据隔离（不入 shoot_batches/shoot_tasks，落 runs 型实验记录）。
- [ ] 导航更名：原「工作台」→「设计方案工作台」（路由 / 不变，仅改名；夹具、历史 run 等全部不动）。

## 5. 核心流程与状态机

```
Fitting Room 勾选 look ──▶ 创建拍摄批次 shoot_batch
   每个 look 生成一条 shoot_task：
   pending ─▶ hero_running ─▶ hero_ready ──(人工确认)──▶ extend_running ─▶ extend_ready ─▶ picking ─▶ done
                   │              │
                   ▼              └─(不满意：换场景/补指令)─▶ 重拍首图（历史 attempt 保留）
                failed（可重试）
```

- 发起校验：look 必须有拍摄槽位（场景图）；缺侧成员必须有补文本，否则该 look 拦下并提示回 Fitting Room 补齐。
- 两段式是硬门控：套图只能从 hero_ready 的确认动作触发；支持批量确认。
- 断点续跑：沿用 shooting 按产物文件名跳过已完成步骤的机制；batch 可整体重试 failed 任务。

## 6. 需求

### 6.1 P0 — 发起拍摄（Fitting Room 侧入口）

- [ ] Fitting Room 看板 look 卡片多选 +「📸 发起拍摄」按钮；波段视图支持"整波段发起"。
- [ ] 发起弹窗：批次名（默认 `{日期}-{波段名/手选}`）、套图数量 k（默认 6）、任务清单预检（逐 look 显示上/下装图、缺侧文本、场景图是否齐备，不齐的行标红且不入批次）。
- [ ] 同一 look 允许多次拍摄（不同批次），历史产物不覆盖。

### 6.2 P0 — 拍摄中心 tab（批次看板 + 首图确认）

- [ ] 批次列表：名称、look 数、状态分布（首图待确认 x / 套图完成 y / 失败 z）、创建时间、预估&实际 token 花费。
- [ ] 批次详情：每 look 一行——参考图组（上/下/场景缩略）｜首图（含历史 attempt 切换）｜套图缩略墙｜状态徽标。
- [ ] 首图操作：「✅ 确认延展」（单个/批量）、「🔁 重拍」（可先更换该 look 拍摄槽位场景图、可附加一句场景指令）、「⏭ 跳过套图」（只要首图直接进选片）。
- [ ] 执行进度走 SSE（复用 generation_tasks 的事件总线模式），行内实时刷新状态。

### 6.3 P0 — 选片

- [ ] 选片模式：该 look 全部产出图（首图 + 套图 + 历史 attempt）平铺，单击 keep/reject，keep 的图可标用途标签（主图 / 详情页 / 小红书，可多选）并拖拽排序。
- [ ] 选片确认后触发 naturalize——**只处理 keep 的图**（去 C2PA / 降生成指纹 / 补相机 EXIF），产出 `_ps.jpg`。
- [ ] 选片结果入库（图 ↔ look ↔ 用途 ↔ 排序），可反复修改；改动后交付包/长图需重新生成（有"已过期"提示）。

### 6.4 P0 — 回流与交付

- [ ] look 详情（Fitting Room 卡片/详情弹窗）展示"📷 可用图组"角标与缩略，点开即选片结果。
- [ ] 详情页长图：把标了「详情页」的图按选片排序纵向拼接（统一宽度 800px、白底、无缝），输出 `{look}_详情长图.jpg`；选中图变化后可一键重拼。
- [ ] 交付包下载：`{look}/`（选中 `_ps.jpg` 按序号命名 + 详情长图 + index.html 离线预览）打 zip；批次级支持整包下载全部已完成 look。
- [ ] 兼容现有「导出到桌面」习惯：交付包可直接落到桌面指定目录（复用 export-to-desktop 通道）。

### 6.5 P1

- 批次级并发与限速配置（防止一次 15 套 look 打爆 API 配额）；成本预估（按 shoot 实测：首图 ≈ 29k input tok 推理 + 1 张生图；套图 ≈ 22k tok + k 张生图）。
- 访客（运营公网账号）开放拍摄中心只读 + 交付包下载；选片写权限是否开放见 Q2。
- 选片图打水印预览（防止未 naturalize 的原图外流）。

### 6.6 P2（预留）

- 多场景候选（一 look 多版首图）；AI 辅助选片排序；详情页长图模板化（头图/尺码表/卖点区块）；与 xhs-taobao-content 技能衔接自动出上架内容包。

## 7. 技术方案

### 7.1 执行架构（已定：并入后端，与设计生图分离）

- shooting 的三个 prompt 框架文件并入 `prompts/shooting/`；`step2_scene / step3_scene_generate / step3.1_extend / step3.2_extend_generate / naturalize` 改造为可编程调用的库函数放 `tool/shooting/`（去 CSV 化：输入直接吃 look 数据结构）。
- 新增 `backend/routers/shoot.py` + 后台线程 runner（复制 generation_tasks 的执行/SSE/断点模式，不共用其表和队列）。
- 产物落盘 `data/shoot/{batch_id}/{look_id}/`，静态挂载 `/static/shoot`。
- 图片输入：look 成员图来自选款中心（生成图取 task_images，上传图取 selection_uploads），场景图来自模板库/用户上传——runner 统一解析为本地绝对路径后喂框架。

### 7.2 数据模型（新表，均不动现有表）

```sql
shoot_batches(id PK, name, wave_id, k INT, status, created_at)
shoot_tasks(id PK, batch_id FK, look_id, status, inputs_json,   -- 上/下装图路径+文本+场景图快照
            hero_attempt INT, error, token_usage_json, updated_at)
shoot_images(id PK, task_id FK, kind,        -- hero | extend
             attempt INT, plan_no, plan_desc, path, ps_path,
             pick TEXT DEFAULT 'pending',    -- pending | keep | reject
             usages TEXT,                    -- JSON: ["main","detail","xhs"]
             sort_order INT, created_at)
```

- inputs_json 是发起时的快照——look 后续在 Fitting Room 被改不影响已建批次（拍摄可追溯）。
- "商品可用图组" v1 = look 维度（`shoot_images WHERE pick='keep'` 按 look 聚合）；是否拆到单品（上装/下装各自详情页）见 Q1。

### 7.3 接口草案

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /shoot/batches | 发起批次（look_ids, k, name）含预检 |
| GET | /shoot/batches / {id} | 列表 / 详情（含各 task 状态与产物） |
| POST | /shoot/tasks/{id}/confirm-hero | 确认首图 → 延展套图（支持批量端点） |
| POST | /shoot/tasks/{id}/retake-hero | 重拍首图（可带新场景图/补充指令） |
| POST | /shoot/tasks/{id}/picks | 提交选片（keep/reject/usages/排序）→ 触发 naturalize |
| POST | /shoot/tasks/{id}/detail-long | 生成/重拼详情页长图 |
| GET | /shoot/tasks/{id}/package | 交付包 zip（批次级同理） |
| GET | /looks/{id}/gallery | 该 look 可用图组（回流查询，Fitting Room 用） |

## 8. 成功指标

- 领先：发起→首图确认的批次完成率 ≥ 90%；首图一次通过率（不重拍占比）基线化后逐版提升；套图浪费率（延展后 0 keep 的 look 占比）< 10%。
- 滞后：单 look 从组套完成到交付包产出 ≤ 1 天（现离线流程 2-3 天）；详情页长图 100% 系统生成，PS 手工拼图归零。

## 9. 开放问题

| # | 问题 | 决策人 | 阻塞性 |
|---|---|---|---|
| Q1 | 可用图组挂 look 还是拆到单品？一套 look 含上装+下装两个商品，单品详情页可能只要该品相关镜头。v1 建议 look 维度 + 用途标签过渡，验证后再拆 | 用户 | 非阻塞（v1 按 look） |
| Q2 | 公网访客（运营）是否开放选片写权限（keep/reject/用途），还是只读+下载？ | 用户 | 非阻塞（M3 前定） |
| Q3 | 详情页长图规格：宽 800px/白底/无缝是否符合店铺要求？要不要留间距/页眉页脚位？ | 用户 | 非阻塞（默认可改） |
| Q4 | k 默认 6 是否合适？批次并发默认 2 个 look 同时跑是否可接受（速度 vs 配额）？ | 用户 | 非阻塞 |

## 10. 里程碑

1. **M1 后端骨架**：shooting 框架库化（去 CSV 化）+ 首图/套图链路可编程调用 + prompt 纳入版本管理。
2. **M2 拍摄工作台**：单 look 实验界面（输入面板 + 节点 trace + 版本 A/B）——先用它把并库后的链路调通验对，再上量产。同步完成导航更名「设计方案工作台」。
3. **M3 拍摄中心**：三表 + 批次 runner + SSE + 批次看板 + 首图确认门控 + Fitting Room 发起入口。
4. **M4 选片+回流**：选片界面、naturalize（仅 keep）、可用图组回流、详情页长图、交付包。
