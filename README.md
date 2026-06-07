# prompt-refine-agent

> 童装牛仔 Step 2 prompt 迭代与 agentic workflow review 工作台。
> 上游依赖 [`ai-supply`](../ai-supply/) 的趋势报告、款图、placement_schema；本项目专注于 **prompt 调试 + 多版本 A/B 对比 + trace 可观测性**。

---

## 一、产品定位

| 项目 | 用途 |
|---|---|
| [`ai-supply/`](../ai-supply/) | 生产流水线：单 prompt step2 + 批量生图，**最终交付物在这里** |
| **`prompt-refine-agent/`（本项目）** | **prompt 迭代工作台**：拆成 7 节点 agent loop，单页交互式调试 + 版本管理 + Fixture 对比 |

阶段一（已完成）：CLI 跑通 agentic step2，验证拆分等价。
阶段二（进行中）：FastAPI 接口 + React/AntD 单页前端，让 prompt 调试有 GUI。
阶段三：批量产能 + 多人协作，回灌生产线。

---

## 二、技术栈

- **后端**：Python 3.10+ / FastAPI / SQLite（只存 runs / prompt_versions / fixtures 元数据）
- **前端**：React 18 / Vite / TypeScript / Ant Design / TanStack Query / Zustand
- **Agent 核心**：OpenAI Responses API（流式）+ 4 节点纯 Python 审计 tool
- **数据**：文件系统是真理源（trace.jsonl / 节点 IO JSON / 最终 step2 JSON 全在 `runs/` 下）

---

## 三、目录结构

```
prompt-refine-agent/
├── README.md                            ← 本文件
├── requirements.txt                     ← Python 依赖
├── .env.example                         ← 复制为 .env 填 OPENAI_API_KEY
├── .gitignore
│
├── run_agent.py                         ← CLI 入口（无需 backend 也能跑）
│
├── prompts/                             ← 所有 prompt（按版本管理）
│   ├── step1/
│   │   └── trend_parse.md
│   ├── step2/
│   │   ├── 01_style_analysis.md          # 2.1
│   │   ├── 02_color_recognition.md       # 2.2
│   │   ├── 03_gender_planning.md         # 2.3
│   │   ├── 04_single_color_design.md     # 2.4
│   │   └── 04b_redesign_constraint.md    # 2.7
│   ├── step3/                            # 待加
│   └── placement_schema.json             # 待从 ai-supply 复制（用 bootstrap.py）
│
├── tool/                                ← Python 包：agent 编排 + 老 step 脚本
│   ├── step0_pdf2images.py
│   ├── step1_trend.py
│   ├── step3_generate.py
│   └── orchestrator/                    ← Step2 7 节点编排器
│       ├── __init__.py
│       ├── orchestrator.py              # Step2Run
│       ├── nodes.py                     # 7 节点执行函数
│       ├── audit.py                     # 4 项纯 Python 审计
│       ├── prompts.py                   # prompt 加载 + 版本绑定
│       ├── trace.py                     # TraceEvent + 节点 IO 缓存
│       └── llm.py                       # OpenAI Responses 薄包装（懒初始化）
│
├── backend/                             ← FastAPI 服务
│   ├── main.py                          # 应用入口
│   ├── db.py                            # SQLite schema
│   ├── schemas.py                       # Pydantic 模型
│   ├── sse.py                           # SSE 事件总线
│   └── routers/
│       ├── runs.py                      # /api/v1/runs（含 fork、retry、SSE）
│       ├── prompts.py                   # /api/v1/prompts（5 节点 + 版本）
│       ├── fixtures.py                  # /api/v1/fixtures
│       ├── trends.py                    # /api/v1/trends（只读，指向 ai-supply）
│       ├── styles.py                    # /api/v1/styles（同上）
│       └── settings.py                  # /api/v1/settings（.env 读写）
│
├── frontend/                            ← React + Vite + AntD
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx                      # 路由 + 顶部导航
│       ├── global.css
│       ├── api/
│       │   └── client.ts                # axios + SSE 订阅
│       ├── store/
│       │   └── runStore.ts              # zustand：当前 run + trace 流
│       ├── routes/
│       │   ├── WorkbenchPage.tsx        # 主工作台（三栏）
│       │   ├── FixturesPage.tsx
│       │   ├── PromptsPage.tsx
│       │   └── SettingsPage.tsx
│       └── components/
│           ├── InputsPanel/             # 左栏：选趋势/款号/性别比/K
│           ├── TraceTree/               # 中栏：实时 trace 树
│           ├── NodeDetail/              # 右栏：节点 prompt + 输出 + Fork 按钮
│           └── FaceVisualizer/          # 三面 SVG 人形锚点示意
│
├── runs/                                ← 运行产物（gitignored）
│   └── {trend}_{style}/
│       ├── {trend}_{style}.json         # 最终聚合输出
│       └── runs/{run_id}/
│           ├── trace.jsonl
│           └── {node_id}_*.json         # 节点 IO 缓存
│
└── data/                                ← 本地元数据（gitignored）
    └── metadata.db
```

---

## 四、上游数据来源（不复制，路径引用）

prompt-refine-agent **不重复存** 数据，全通过路径引用：

| 数据类型 | 默认位置 | 覆盖方式 |
|---|---|---|
| 趋势报告 PDF / 解析 JSON | `../ai-supply/趋势报告/` | 环境变量 `TRENDS_ROOT` |
| 款图 + 色号图 | `../ai-supply/款图/` | 环境变量 `STYLES_ROOT` |
| `placement_schema.json` | `../ai-supply/prompt/placement_schema.json` | 待加 bootstrap.py |

---

## 五、快速开始

### 1. 准备依赖

```bash
cd prompt-refine-agent
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY
```

### 2. CLI 验证（无需起前端）

```bash
python run_agent.py \
    --trend-json   "../ai-supply/趋势报告/山系户外童装花型TOP热榜/山系户外童装花型TOP热榜.json" \
    --ref-image    "../ai-supply/款图/YK250609-牛仔服.jpg" \
    --color-folder "../ai-supply/款图/YK250609-牛仔服" \
    --gender-ratio "男女比接近1:1" \
    --num-designs  1 \
    --dry-run
```

dry-run 跑通后去掉 `--dry-run` 跑真。输出落到 `runs/{trend}_{style}/`。

### 3. 起后端（开发模式）

```bash
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
# 文档：http://127.0.0.1:8000/docs
```

### 4. 起前端（开发模式）

```bash
cd frontend
npm install
npm run dev
# 访问：http://127.0.0.1:5173
```

---

## 六、Step 2 的 7 节点拆分

| 节点 | 类型 | 含义 |
|---|---|---|
| **2.1 款式分析** | 🧠 LLM | 基于款图正反面 + 趋势 JSON 输出款式结构 + 趋势说明 |
| **2.2 颜色识别** | 🧠 LLM × N | 对每张色号图独立识别底色 + 是否需变色（并发） |
| **2.3 性别比规划** | 🧠 LLM | 基于识别色 + 用户比例分配男童/女童/中性 |
| **2.4 单色设计** | 🧠 LLM × N | 对每色出 K 个方案，**串行注入累积状态约束** |
| **2.5 分布审计** | 🔧 Python tool | 4 项硬性规则纯算法判定，0 token |
| **2.6 重做决策** | 🔀 Decision | 审计失败时挑出要重做的色号 + 约束 |
| **2.7 局部重设计** | 🔁 Loop + LLM | 仅对违规色号重新出方案 |

详见 `tool/orchestrator/` 与 `prompts/step2/*.md`。

---

## 七、API 速览

完整 OpenAPI 文档：http://127.0.0.1:8000/docs

| 路径 | 用途 |
|---|---|
| `POST   /api/v1/runs` | 创建一次 run（后台异步执行） |
| `GET    /api/v1/runs/{id}` | 完整详情（含 trace） |
| `GET    /api/v1/runs/{id}/stream` | **SSE 实时事件流** |
| `GET    /api/v1/runs/{id}/nodes/{node_id}` | 单节点 IO 缓存（fork 前查看） |
| `POST   /api/v1/runs/{id}/fork` | 从某节点 fork 重跑 |
| `GET    /api/v1/prompts` | 5 节点 + 各自版本树 |
| `POST   /api/v1/prompts/{node_id}/versions` | 存新版本 |
| `GET    /api/v1/prompts/{node_id}/diff?v1=&v2=` | 版本 diff |
| `GET    /api/v1/fixtures` | 测试夹具列表 |
| `GET    /api/v1/trends` | 浏览 ai-supply 趋势报告 |
| `GET    /api/v1/styles` | 浏览 ai-supply 款图库 |
| `GET    /api/v1/settings` / `PATCH` | 本地 .env 配置 |

---

## 八、Phase 1 完成项 / Phase 2 待办

✅ **完成**
- Step 2 拆分为 7 节点 + 7 Python 模块（`tool/orchestrator/`）
- 5 个子 prompt 文件 + 版本格式约定（`prompts/step2/`）
- 4 项审计 tool（确定性 Python）
- CLI `run_agent.py` 跑通 dry-run（23 节点串通）
- FastAPI 路由骨架（`backend/`）
- 前端工作台/Fixtures/Prompts/Settings 4 页骨架（`frontend/`）

⏳ **Phase 2 待办**
- 把 `POST /runs` 接入 orchestrator（背景 task + TraceWriter callback → event_bus）
- 实现 fork-from-node：复用前置缓存 + 从指定节点重跑
- 前端 trace 树活水流（接 SSE）
- Prompt 编辑抽屉（带 `{{变量}}` 高亮 + 实时渲染预览 + 存版本）
- Fixture 横向对比视图（多 run 并排，自动算指标差值）
- bootstrap.py：一键从 ai-supply 拉 placement_schema.json + 范例趋势报告

---

## 九、与 ai-supply 的关系约定

- **`ai-supply/` 一行不动**——`tool/step2_design.py` 等生产脚本保持原状
- **prompt-refine-agent 只读引用**：趋势报告、款图、placement_schema
- **prompt 迭代成熟后**：把验证过的子 prompt **回灌到 ai-supply 生产线**（手工合并；Phase 3 考虑双写自动化）
