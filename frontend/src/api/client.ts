import axios from "axios";

export const api = axios.create({
  baseURL: "/api/v1",
  timeout: 30_000,
});

export type RunSummary = {
  id: string;
  fixture_id: string | null;
  trend_name: string;
  style_no: string;
  gender_ratio: string;
  num_designs_k: number;
  status: "running" | "succeeded" | "failed";
  audit_passed: boolean | null;
  audit_rounds: number | null;
  total_tokens_in: number | null;
  total_tokens_out: number | null;
  elapsed_ms: number | null;
  parent_run_id: string | null;
  fork_from_node: string | null;
  created_at: string;
};

export type TraceEvent = {
  event: string;
  timestamp: number;
  node_id?: string;
  parent_id?: string;
  node_type?: "llm" | "tool" | "decision" | "loop" | "run";
  name?: string;
  prompt_version?: string;
  delta?: string;
  output_summary?: Record<string, unknown>;
  tokens?: { input?: number; output?: number };
  elapsed_ms?: number;
  error?: string;
  status?: string;
  [extra: string]: unknown;
};


// ----- Runs ----- //
export const listRuns = () => api.get<RunSummary[]>("/runs").then((r) => r.data);
export const getRun = (id: string) => api.get(`/runs/${id}`).then((r) => r.data);
export const getNode = (runId: string, nodeId: string) =>
  api.get(`/runs/${runId}/nodes/${nodeId}`).then((r) => r.data);
export const createRun = (payload: Record<string, unknown>) =>
  api.post<RunSummary>("/runs", payload).then((r) => r.data);
export const forkRun = (
  runId: string,
  payload: {
    from_node_id: string;
    prompt_overrides?: Record<string, string>;
    description?: string;
  },
) => api.post<RunSummary>(`/runs/${runId}/fork`, payload).then((r) => r.data);

// ----- 单色重识别（针对 2.2 失败色号原地修复） ----- //
export const recognizeColor = (runId: string, payload: { color_code: string; color_name: string }) =>
  api.post(`/runs/${runId}/recognize-color`, payload).then((r) => r.data);

// ----- 单色重设计（重识别后让该色号的 2.4 方案也跟着新识别色重做） ----- //
export const redesignSingleColor = (runId: string, payload: { color_code: string; color_name: string }) =>
  api.post(`/runs/${runId}/redesign-single-color`, payload).then((r) => r.data);

// ----- SSE ----- //
export const subscribeRun = (
  runId: string,
  onEvent: (event: string, data: TraceEvent) => void,
): EventSource => {
  const es = new EventSource(`/api/v1/runs/${runId}/stream`);
  // 监听所有事件类型
  ["run_started", "node_started", "node_streaming", "node_completed", "node_failed",
    "decision_made", "loop_iteration", "retry_triggered", "run_finished", "error",
  ].forEach((name) => {
    es.addEventListener(name, (e: MessageEvent) => {
      try {
        onEvent(name, JSON.parse(e.data));
      } catch (err) {
        console.warn("SSE parse error", err);
      }
    });
  });
  return es;
};

// ----- Prompts ----- //
export const listPrompts = () => api.get("/prompts").then((r) => r.data);
export const getPrompt = (nodeId: string, version = "current") =>
  version === "current"
    ? api.get(`/prompts/${nodeId}`).then((r) => r.data)
    : api.get(`/prompts/${nodeId}/versions/${version}`).then((r) => r.data);
export const savePromptVersion = (
  nodeId: string,
  payload: { new_content: string; description?: string; save_as?: string },
) => api.post(`/prompts/${nodeId}/versions`, payload).then((r) => r.data);
export const diffPrompts = (nodeId: string, v1: string, v2: string) =>
  api.get(`/prompts/${nodeId}/diff`, { params: { v1, v2 } }).then((r) => r.data);

// ----- Fixtures ----- //
export const listFixtures = () => api.get("/fixtures").then((r) => r.data);
export const createFixture = (payload: Record<string, unknown>) =>
  api.post("/fixtures", payload).then((r) => r.data);

// ----- Compare runs ----- //
export const compareRuns = (runIds: string[]) =>
  api.get(`/runs/compare/multi`, { params: { ids: runIds.join(",") } }).then((r) => r.data);

// ----- M6/M7 生图任务 ----- //
export type TaskSummary = {
  id: string;
  source_step2_run_id: string;
  trend_name: string;
  style_no: string;
  status: "pending" | "running" | "completed" | "partial" | "failed";
  progress_done: number;
  progress_total: number;
  total_elapsed_ms?: number | null;
  total_cost_usd?: number | null;
  created_at: string;
  completed_at?: string | null;
  // 来自源 step2 run 的 prompt_bundle，形如 {"2.1": "01_style_analysis.md@current", ...}
  source_prompt_bundle?: Record<string, string> | null;
};

export const listTasks = () => api.get<TaskSummary[]>("/tasks").then((r) => r.data);
export const getTask = (id: string) => api.get(`/tasks/${id}`).then((r) => r.data);
export const createTask = (payload: {
  source_step2_run_id: string;
  selected_plan_ids: string[];
  concurrency?: number;
}) => api.post<TaskSummary>("/tasks", payload).then((r) => r.data);
export const regenerateTask = (id: string, payload: { plan_ids: string[]; concurrency?: number }) =>
  api.post<TaskSummary>(`/tasks/${id}/regenerate`, payload).then((r) => r.data);
export const deleteTask = (id: string) => api.delete(`/tasks/${id}`).then((r) => r.data);
export const prepTaskFromRun = (runId: string) =>
  api.get(`/tasks/prep/from-run/${runId}`).then((r) => r.data);

export const compareTasks = (taskIds: string[]) =>
  api.get(`/tasks/compare/multi`, { params: { ids: taskIds.join(",") } }).then((r) => r.data);

export const subscribeTask = (taskId: string, onEvent: (eventType: string, data: any) => void): EventSource => {
  const es = new EventSource(`/api/v1/tasks/${taskId}/stream`);
  ["task_started", "task_size_chosen", "plan_started", "plan_succeeded", "plan_failed", "task_finished"].forEach((name) => {
    es.addEventListener(name, (e: MessageEvent) => {
      try {
        onEvent(name, JSON.parse(e.data));
      } catch (err) {
        console.warn("SSE parse error", err);
      }
    });
  });
  return es;
};

// ----- Trends / Styles ----- //
export const listTrends = () => api.get("/trends").then((r) => r.data);
export const getTrend = (name: string) =>
  api.get(`/trends/${encodeURIComponent(name)}`).then((r) => r.data);
export const listStyles = () => api.get("/styles").then((r) => r.data);
export const getStyle = (styleNo: string) =>
  api.get(`/styles/${encodeURIComponent(styleNo)}`).then((r) => r.data);

// ----- Settings ----- //
export const getSettings = () => api.get("/settings").then((r) => r.data);
export const updateSettings = (payload: Record<string, unknown>) =>
  api.patch("/settings", payload).then((r) => r.data);
