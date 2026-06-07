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

// ----- Trends / Styles ----- //
export const listTrends = () => api.get("/trends").then((r) => r.data);
export const getTrend = (name: string) =>
  api.get(`/trends/${encodeURIComponent(name)}`).then((r) => r.data);
export const listStyles = () => api.get("/styles").then((r) => r.data);
export const getStyle = (id: string) =>
  api.get(`/styles/${encodeURIComponent(id)}`).then((r) => r.data);

// ----- Settings ----- //
export const getSettings = () => api.get("/settings").then((r) => r.data);
export const updateSettings = (payload: Record<string, unknown>) =>
  api.patch("/settings", payload).then((r) => r.data);
