import axios from "axios";

export const api = axios.create({
  baseURL: "/api/v1",
  timeout: 30_000,
});

export type DesignMode = "MULTI_TOPIC" | "SINGLE_TOPIC_STRONG" | "COLLECTION_2SKU";

export type RunSummary = {
  id: string;
  fixture_id: string | null;
  trend_name: string;
  style_no: string;
  gender_ratio: string;
  num_designs_k: number;
  design_mode?: DesignMode;
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
export const getFixture = (id: string) => api.get(`/fixtures/${id}`).then((r) => r.data);
export const createFixture = (payload: Record<string, unknown>) =>
  api.post("/fixtures", payload).then((r) => r.data);
export const deleteFixture = (id: string) =>
  api.delete(`/fixtures/${id}`).then((r) => r.data);

// ----- Compare runs ----- //
export const compareRuns = (runIds: string[]) =>
  api.get(`/runs/compare/multi`, { params: { ids: runIds.join(",") } }).then((r) => r.data);

// ----- v5 CONVERGE（强单主题 / Collection） ----- //
export const getRunFinal = (runId: string) =>
  api.get(`/runs/${runId}/final`).then((r) => r.data);
// 手动触发：把成套 run 已生成的整套 look 同步到 Fitting Room（幂等）
export const syncCollectionLooks = (runId: string) =>
  api.post<{ ok: boolean; created: number }>(`/tasks/sync-collection-looks/${runId}`).then((r) => r.data);

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
// 跨任务全局图片墙
export const listAllImages = (limit = 200) =>
  api.get("/tasks/all-images", { params: { limit } }).then((r) => r.data);

// ----- Fitting Room ----- //
export type Look = {
  id: string;
  name: string;
  top_kind: "image" | "text" | null;
  top_image_id: string | null;
  top_text: string | null;
  bottom_kind: "image" | "text" | null;
  bottom_image_id: string | null;
  bottom_text: string | null;
  tags: string[];
  shooting_slot_kind: "upload" | "template" | null;
  shooting_slot_url: string | null;
  shooting_slot_meta: Record<string, any> | null;
  wave_id: string | null;          // 波段归属；null = 未分波段
  created_at: string;
  updated_at: string;
};
export type LookCreateItem = {
  name?: string | null;
  top_kind?: "image" | "text" | null;
  top_image_id?: string | null;
  top_text?: string | null;
  bottom_kind?: "image" | "text" | null;
  bottom_image_id?: string | null;
  bottom_text?: string | null;
  tags?: string[];
};

export const listLooks = () => api.get<Look[]>("/looks").then((r) => r.data);
export const createLooksBulk = (looks: LookCreateItem[]) =>
  api.post<Look[]>("/looks", { looks }).then((r) => r.data);
export const updateLook = (id: string, payload: Partial<LookCreateItem>) =>
  api.patch<Look>(`/looks/${id}`, payload).then((r) => r.data);
export const deleteLook = (id: string) =>
  api.delete(`/looks/${id}`).then((r) => r.data);
export const duplicateLook = (id: string) =>
  api.post<Look>(`/looks/${id}/duplicate`).then((r) => r.data);
// 触发浏览器下载 JSON
export const exportLooksJson = () => {
  window.open("/api/v1/looks/export", "_blank");
};

// 批量导出选中的 look 图片到用户桌面
export type LooksExportResponse = {
  ok: boolean;
  output_dir: string;
  csv_path: string | null;
  exported_count: number;
  exported: Array<{ look_id: string; look_name: string; files: string[]; partial_skips: string[] }>;
  skipped: Array<{ look_id: string; look_name?: string; reason: string }>;
};
export const exportLooksToDesktop = (lookIds: string[]) =>
  api
    .post<LooksExportResponse>("/looks/export-to-desktop", { look_ids: lookIds })
    .then((r) => r.data);

// ── 选款中心（已确认上架的 SKU 仓库）──────────────────────────
export type SelectionStyle = {
  id: string;
  image_id: string;                    // generated: task:plan；uploaded: upload:{uuid}
  source_kind: "generated" | "uploaded";
  category: "top" | "bottom" | null;
  style_no: string | null;
  color_code: string | null;
  color_name: string | null;
  note: string | null;
  origin: string;                      // 手动选款 | 存量迁移 | 成套联动 | 上传
  upload_url: string | null;
  created_at: string;
};
export const listSelection = () =>
  api.get<SelectionStyle[]>("/selection").then((r) => r.data);
export const listSelectionImageIds = () =>
  api.get<{ image_ids: string[] }>("/selection/image-ids").then((r) => r.data.image_ids);
export const addSelection = (items: Array<{
  image_id: string; category?: string | null;
  style_no?: string | null; color_code?: string | null; color_name?: string | null;
}>) => api.post("/selection", { items }).then((r) => r.data);
export const uploadSelection = (
  file: File,
  meta: { category: "top" | "bottom"; style_no: string; color_name?: string; note?: string },
) => {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("category", meta.category);
  fd.append("style_no", meta.style_no);
  if (meta.color_name) fd.append("color_name", meta.color_name);
  if (meta.note) fd.append("note", meta.note);
  return api.post<SelectionStyle>("/selection/upload", fd).then((r) => r.data);
};
export const removeSelection = (id: string) =>
  api.delete(`/selection/${id}`).then((r) => r.data);

// ── 波段上新管理 ──────────────────────────────────────────────
export type Wave = {
  id: string;
  name: string;
  planned_launch_date: string | null;   // YYYY-MM-DD
  status: string;                       // 规划中 | 已上架
  created_at: string;
  updated_at: string;
  look_count: number;
  style_count: number;                  // 波段内款色图去重数
};
export const listWaves = () => api.get<Wave[]>("/waves").then((r) => r.data);
export const createWave = (payload: {
  name: string; planned_launch_date?: string | null; look_ids?: string[];
}) => api.post<Wave>("/waves", payload).then((r) => r.data);
export const updateWave = (id: string, payload: {
  name?: string; planned_launch_date?: string; status?: string;
}) => api.patch<Wave>(`/waves/${id}`, payload).then((r) => r.data);
export const deleteWave = (id: string) =>
  api.delete(`/waves/${id}`).then((r) => r.data);
export const assignLooksToWave = (waveId: string, lookIds: string[]) =>
  api.post(`/waves/${waveId}/looks`, { look_ids: lookIds }).then((r) => r.data);
export const unassignLooksFromWave = (lookIds: string[]) =>
  api.post("/waves/unassign", { look_ids: lookIds }).then((r) => r.data);

// 上装 / 下装归类
export const listImageCategories = () =>
  api.get<Record<string, "top" | "bottom">>("/image-categories").then((r) => r.data);
export const bulkUpsertImageCategories = (
  items: { image_id: string; category: "top" | "bottom"; source?: string }[],
) => api.post("/image-categories", { items }).then((r) => r.data);

// ----- 印花图案库（v6 图库输入源）----- //
export type PatternLibraryGroup = {
  folder_name: string;
  parent_topic: string;
  sub_topic: string;
  image_count: number;
  cover_url: string;
  images: Array<{ filename: string; url: string }>;
};
export type PatternLibraryData = {
  root: string;
  available: boolean;
  error?: string;
  categories: string[];
  groups: PatternLibraryGroup[];
  meta: { group_count: number; image_count: number };
};
export const listPatternLibrary = () =>
  api.get<PatternLibraryData>("/pattern-library").then((r) => r.data);
export const getPatternLibraryStats = () =>
  api.get("/pattern-library/stats").then((r) => r.data);

// ----- 视觉模板库 & 拍摄槽位 ----- //
export type TemplatesData = {
  meta: { categories: string[]; group_count?: number; image_count?: number; generated_at?: string };
  tag_dict: { schemas: Record<string, any> };
  groups: Array<{
    category: string;
    name: string;
    model_name?: string;
    model_type?: string;
    scene_desc?: string;
    group_tags?: Record<string, string[]>;
    images: Array<{ file?: string; filename?: string; tags?: Record<string, string> }>;
  }>;
  error?: string;
  root?: string;
};
export type TemplatesStats = {
  available: boolean;
  root?: string;
  categories?: string[];
  group_count?: number;
  image_count?: number;
  generated_at?: string;
};
export const getTemplatesData = () => api.get<TemplatesData>("/templates").then((r) => r.data);
export const getTemplatesStats = () => api.get<TemplatesStats>("/templates/stats").then((r) => r.data);

export const uploadShootingSlot = (lookId: string, file: File, remark?: string) => {
  const form = new FormData();
  form.append("file", file);
  if (remark) form.append("remark", remark);
  return api
    .post(`/looks/${lookId}/shooting-slot/upload`, form, {
      headers: { "Content-Type": "multipart/form-data" },
    })
    .then((r) => r.data);
};

export const setShootingSlotTemplate = (
  lookId: string,
  payload: {
    category: string;
    group_name: string;
    image_filename: string;
    group_tags?: Record<string, any>;
    image_tags?: Record<string, any>;
    remark?: string;
  },
) =>
  api
    .post(`/looks/${lookId}/shooting-slot/set-template`, payload)
    .then((r) => r.data);

export const clearShootingSlot = (lookId: string) =>
  api.delete(`/looks/${lookId}/shooting-slot`).then((r) => r.data);
export const getTask = (id: string) => api.get(`/tasks/${id}`).then((r) => r.data);
export const createTask = (payload: {
  source_step2_run_id: string;
  selected_plan_ids: string[];
  concurrency?: number;
}) => api.post<TaskSummary>("/tasks", payload).then((r) => r.data);
export const regenerateTask = (
  id: string,
  payload: { plan_ids: string[]; concurrency?: number; keep_original?: boolean },
) => api.post<TaskSummary>(`/tasks/${id}/regenerate`, payload).then((r) => r.data);
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

// 上传 PDF + 名字 → 启动趋势导入 job
export const uploadTrend = (name: string, pdf: File) => {
  const form = new FormData();
  form.append("name", name);
  form.append("pdf", pdf);
  return api.post("/trends/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
  }).then((r) => r.data);
};

// 订阅趋势导入 job SSE 进度
export const subscribeTrendImport = (
  jobId: string,
  onEvent: (event: string, payload: any) => void,
): EventSource => {
  const es = new EventSource(`/api/v1/trends/jobs/${jobId}/stream`);
  ["trend_started", "trend_progress", "trend_succeeded", "trend_failed"].forEach((name) => {
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
// ----- 账单（token / 成本统计） ----- //
export const getBillingSummary = (params?: { date_from?: string; date_to?: string }) =>
  api.get("/billing/summary", { params }).then((r) => r.data);
export const getBillingDelivery = (mainRunId: string) =>
  api.get(`/billing/delivery/${mainRunId}`).then((r) => r.data);

// ----- Settings ----- //
export const getSettings = () => api.get("/settings").then((r) => r.data);
export const updateSettings = (payload: Record<string, unknown>) =>
  api.patch("/settings", payload).then((r) => r.data);

// ----- 款级缓存管理（2.1 款式分析 / 2.2 颜色识别 跨 run 复用） ----- //
export type StyleCacheStats = {
  root: string;
  total_bytes: number;
  styles: Array<{
    style_no: string;
    analysis_count: number;
    color_count: number;
    total_bytes: number;
    updated_at: string;
  }>;
};
export const getStyleCacheStats = () =>
  api.get<StyleCacheStats>("/settings/style-cache").then((r) => r.data);
export const clearStyleCache = (styleNo?: string) =>
  (styleNo
    ? api.delete(`/settings/style-cache/${encodeURIComponent(styleNo)}`)
    : api.delete("/settings/style-cache")
  ).then((r) => r.data);
