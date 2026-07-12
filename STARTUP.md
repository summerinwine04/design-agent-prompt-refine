# 启动 cheatsheet

> 项目内启动 / 排错速查。每次开机就照这份做。

---

## 启动两个服务

**两个独立 PowerShell 窗口**（按 `Win+R` → 输入 `powershell` 各开一个；**不要**用 VS Code 内置 terminal —— PSReadLine 经常崩）。

### 窗口 1 — 后端

```powershell
cd C:\Users\zgj\Documents\Claude\Projects\summer\prompt-refine-agent
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --reload --port 8000
```

**判断成功**：prompt 前面出现 `(.venv)` + 看到这两行：

```
[startup] .env loaded  OPENAI_API_KEY=set ✓
Uvicorn running on http://127.0.0.1:8000
```

### 窗口 2 — 前端

```powershell
cd C:\Users\zgj\Documents\Claude\Projects\summer\prompt-refine-agent\frontend
npm run dev
```

**判断成功**：看到 `Local: http://localhost:5173/`

### 浏览器

打开 **http://localhost:5173**

---

## 重要：这两个窗口都不能关

| 窗口 | 关了的后果 |
|---|---|
| 后端 | 浏览器拿不到数据，工作台空白 / API 报错 |
| 前端 | 浏览器打不开 5173 端口（`ERR_CONNECTION_REFUSED`） |

要跑 git / curl / pip 之类的命令，**再开第 3 个窗口**，不要占用上面两个。

临时停服务：在对应窗口按 `Ctrl + C`。重启就再跑同样的命令。

---

## 常见踩坑速查

| 报错 | 真因 | 修法 |
|---|---|---|
| `Fatal error in launcher: Unable to create process` | `uvicorn.exe` launcher 损坏 | 用 `python -m uvicorn ...` 替代 `uvicorn ...`（上面已经是）|
| `No module named uvicorn` / `No module named backend` | 没激活 venv 或不在项目根 | 重新走 cd + Activate 两步 |
| `Activate.ps1 cannot be loaded` | PowerShell 执行策略限制 | 一次性绕过：`powershell -ExecutionPolicy Bypass -File .\.venv\Scripts\Activate.ps1` |
| `Address already in use` | 上次 uvicorn 没关干净 | `Get-Process python \| Stop-Process -Force` 然后重启 |
| 前端打不开 / 趋势下拉空 | 一个窗口挂了 | F12 → Network 看哪边 404，重启对应窗口 |
| PSReadLine 屏幕错乱报红字 | PowerShell 渲染 bug，跟代码无关 | 命令其实跑了；要么忽略，要么再开个新 PowerShell 窗口 |
| 设置页改了 API Key 但仍 `quota exceeded` | 同账户两个 key | 去 OpenAI billing 充值（不是换 key）|
| 趋势上传 `PermissionError` | PDF 被 Adobe Reader / 资源管理器预览占用 | 关掉打开 PDF 的程序，或改个名 |
| 生图正反面袖子被裁切 | gpt-image-2 没听 LAYOUT guard | 重生 / 调 step2 prompt（v4 可调主题选择 / 单色设计 prompt） |

---

## 验证 backend 是不是真的在跑

新开窗口跑：

```powershell
curl.exe http://127.0.0.1:8000/api/v1/trends
```

返回 JSON `{"root": "...", "trends": [...]}` → OK。
返回 `curl: (7) Failed to connect` → 后端没在跑，重启窗口 1。

---

## 一键停掉所有 python / node 进程（重启大法）

```powershell
Get-Process python, node -ErrorAction SilentlyContinue | Stop-Process -Force
```

然后按上面的步骤重新起两个窗口。

---

## 关键路径

- 项目根：`C:\Users\zgj\Documents\Claude\Projects\summer\prompt-refine-agent`
- 前端：`<root>\frontend\`
- venv：`<root>\.venv\`
- 数据：`<root>\..\ai-supply\` 趋势报告 + 款图
- DB：`<root>\data\metadata.db`
- 生成的 run：`<root>\runs\`
- 生成的图：`<root>\data\task_images\`
- 关键配置：`<root>\.env`（OpenAI Key、POPPLER_PATH 等）

---

## 当前节点结构（v4）

```
2.1 款式分析（只看款图）
  ↓
2.2 颜色识别（每色一次，并发）
  ↓
2.3 性别比规划（男童 / 女童 / 中性，输入是三栏 %）
  ↓
2.4 趋势主题选择（选 2-3 个 + 色号映射）
  ↓
2.5 单色设计（串行，吃分配主题，不再吃完整趋势）
  ↓
2.6 审计 → 2.7 决策 → 2.8 局部重设计（loop）
  ↓
最终方案 → step3 图生图（gpt-image-2）
```

可编辑 prompt 的 6 个 LLM 节点：**2.1 / 2.2 / 2.3 / 2.4 / 2.5 / 2.8**。

---

_最后更新：v4 节点重构完成（2.4 主题选择独立节点；性别去掉"双性"；单色设计不再吃完整趋势）_
