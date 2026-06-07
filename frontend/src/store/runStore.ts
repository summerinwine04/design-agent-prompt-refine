import { create } from "zustand";

import { createRun, forkRun, getRun, subscribeRun, type TraceEvent } from "../api/client";

type RunStatus = "idle" | "running" | "succeeded" | "succeeded_with_audit_warnings" | "failed";

interface RunStore {
  // 当前 run
  currentRunId: string | null;
  status: RunStatus;
  isRunning: boolean;            // 派生：status === "running"

  // 事件流
  trace: TraceEvent[];
  eventSource: EventSource | null;

  // UI 状态
  selectedNodeId: string | null;
  error: string | null;

  // M3：用户在 PromptDrawer 里点"用此版本跑"会写到这里
  //   key = "2.1" / "2.2" / ...，value = "current" / "v2" / ...
  promptOverrides: Record<string, string>;

  // actions
  startRun: (payload: Record<string, unknown>) => Promise<void>;
  forkFromCurrent: (fromNodeId: string, promptOverrides: Record<string, string>) => Promise<void>;
  loadRun: (runId: string) => Promise<void>;     // M4.5: 加载历史 run 进 store
  appendEvent: (event: TraceEvent) => void;
  selectNode: (id: string | null) => void;
  reset: () => void;
  setPromptOverride: (nodeBizId: string, version: string) => void;
  clearPromptOverrides: () => void;
}

export const useRunStore = create<RunStore>((set, get) => ({
  currentRunId: null,
  status: "idle",
  isRunning: false,
  trace: [],
  eventSource: null,
  selectedNodeId: null,
  error: null,
  promptOverrides: {},

  async startRun(payload) {
    // 1. 先清掉上一个 run 的状态（保留 promptOverrides 不变）
    const keepOverrides = get().promptOverrides;
    get().reset();
    set({ status: "running", isRunning: true, error: null, promptOverrides: keepOverrides });

    // 2. 注入 promptOverrides 到 prompt_bundle
    const overrides = get().promptOverrides;
    const enrichedPayload = {
      ...payload,
      prompt_bundle: {
        versions: {
          ...(((payload as any).prompt_bundle?.versions) ?? {}),
          ...overrides,
        },
      },
    };

    try {
      const run = await createRun(enrichedPayload);
      set({
        currentRunId: run.id,
        status: (run.status as RunStatus) || "running",
        trace: [],
      });

      // 3. 订阅 SSE
      const es = subscribeRun(run.id, (_eventType, data) => {
        get().appendEvent(data);

        // run_finished → 自动关闭订阅，更新状态
        if (data.event === "run_finished") {
          const finalStatus = (data.status as RunStatus) || "succeeded";
          set({
            status: finalStatus,
            isRunning: false,
          });
          es.close();
          set({ eventSource: null });
        }

        // node_failed 记录
        if (data.event === "node_failed") {
          console.warn("Node failed:", data);
        }
      });

      es.onerror = (e) => {
        console.error("SSE error:", e);
        if (es.readyState === EventSource.CLOSED) {
          set({ error: "SSE 连接断开", isRunning: false, status: "failed" });
        }
      };

      set({ eventSource: es });
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || String(err);
      console.error("createRun failed:", err);
      set({ error: msg, isRunning: false, status: "failed" });
    }
  },

  async forkFromCurrent(fromNodeId, promptOverrides) {
    const srcRunId = get().currentRunId;
    if (!srcRunId) {
      set({ error: "没有可 fork 的 currentRunId" });
      return;
    }
    // 切到新 run（保留 promptOverrides 给后续手动跑用）
    const keepOverrides = get().promptOverrides;
    get().reset();
    set({ status: "running", isRunning: true, error: null, promptOverrides: keepOverrides });

    try {
      const run = await forkRun(srcRunId, {
        from_node_id: fromNodeId,
        prompt_overrides: promptOverrides,
      });
      set({
        currentRunId: run.id,
        status: (run.status as RunStatus) || "running",
        trace: [],
      });
      const es = subscribeRun(run.id, (_eventType, data) => {
        get().appendEvent(data);
        if (data.event === "run_finished") {
          const finalStatus = (data.status as RunStatus) || "succeeded";
          set({ status: finalStatus, isRunning: false });
          es.close();
          set({ eventSource: null });
        }
      });
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) {
          set({ error: "SSE 连接断开", isRunning: false, status: "failed" });
        }
      };
      set({ eventSource: es });
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || String(err);
      console.error("forkRun failed:", err);
      set({ error: msg, isRunning: false, status: "failed" });
    }
  },

  async loadRun(runId) {
    // 加载历史 run：把磁盘上的 trace.jsonl 全装到内存，currentRunId 切到这个 run
    // 不订阅 SSE（因为这个 run 已经结束了）
    const keepOverrides = get().promptOverrides;
    get().reset();
    try {
      const detail: any = await getRun(runId);
      set({
        currentRunId: detail.id,
        status: (detail.status as RunStatus) || "succeeded",
        isRunning: false,
        trace: (detail.trace || []).filter((e: any) => e.event !== "node_streaming"),
        selectedNodeId: null,
        error: null,
        eventSource: null,
        promptOverrides: keepOverrides,
      });
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || String(err);
      set({ error: `加载 run ${runId} 失败：${msg}`, status: "idle", isRunning: false });
    }
  },

  appendEvent(event) {
    // 双重防御：忽略高频流式事件，防止 React 渲染雪崩
    if (event.event === "node_streaming") return;
    set((state) => ({ trace: [...state.trace, event] }));
  },

  selectNode(id) {
    set({ selectedNodeId: id });
  },

  reset() {
    const es = get().eventSource;
    if (es) es.close();
    set({
      currentRunId: null,
      status: "idle",
      isRunning: false,
      trace: [],
      eventSource: null,
      selectedNodeId: null,
      error: null,
      // 注意：reset 不动 promptOverrides；切换 fixture / 手动清才会
    });
  },

  setPromptOverride(nodeBizId, version) {
    set((state) => ({
      promptOverrides: { ...state.promptOverrides, [nodeBizId]: version },
    }));
  },

  clearPromptOverrides() {
    set({ promptOverrides: {} });
  },
}));
