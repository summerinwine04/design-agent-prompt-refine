import { Card, Tag, Space, Spin, Empty, Button, Modal, Collapse, Progress, Alert, message, Tooltip, Segmented } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, Link } from "react-router-dom";
import { useEffect, useState } from "react";

import { getTask, subscribeTask, regenerateTask } from "../api/client";

// 与 TasksListPage 保持一致的 prompt 版本展示规则
// v4 节点
const PROMPT_NODE_LABELS: Record<string, string> = {
  "2.1": "款式分析",
  "2.2": "颜色识别",
  "2.3": "性别规划",
  "2.4": "主题选择",
  "2.5": "单色设计",
  "2.8": "局部重设计",
};
function extractPromptVersion(label: string | undefined): string {
  if (!label) return "?";
  const at = label.lastIndexOf("@");
  return at >= 0 ? label.slice(at + 1) : label;
}

const STATUS_COLOR: Record<string, string> = {
  pending: "default",
  running: "processing",
  completed: "success",
  partial: "warning",
  failed: "error",
};

/**
 * 任务详情页
 * 顶部 banner + Step 1/2/3 折叠面板 + Step 3 图 grid
 * SSE 实时更新进度（决策⑤）
 * 单张图失败可独立重生（决策③）
 */
export default function TaskDetailPage() {
  const { taskId } = useParams<{ taskId: string }>();
  const queryClient = useQueryClient();
  const [imageModalPlanId, setImageModalPlanId] = useState<string | null>(null);
  // Step 3 视图模式：卡片视图（含色号 + 方案文字 + 重生按钮）/ 平铺视图（纯图片墙）
  const [step3ViewMode, setStep3ViewMode] = useState<"card" | "tiled">("card");

  // SSE 实时结果：单图完成事件直接 patch 进来，不等 DB 整批写完
  // 这样首张图 30-60s 后就立刻可见，不必等整批 N×30s
  const [liveResults, setLiveResults] = useState<Map<string, any>>(new Map());

  const { data, isLoading } = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => getTask(taskId!),
    enabled: !!taskId,
    refetchInterval: (q) => (q.state.data as any)?.status === "running" ? 2000 : false,
  });

  // 订阅 SSE：单图事件 patch liveResults + 其他事件 refetch
  useEffect(() => {
    if (!taskId) return;
    if (data && data.status !== "running" && data.status !== "pending") return;
    const es = subscribeTask(taskId, (eventType, payload: any) => {
      // 单图级事件：直接更新 liveResults，让该 plan 的状态/图立即出现
      if (eventType === "plan_started" || eventType === "plan_succeeded" || eventType === "plan_failed") {
        const planId = payload?.plan_id;
        if (planId) {
          setLiveResults((prev) => {
            const next = new Map(prev);
            next.set(planId, {
              plan_id: planId,
              color_code: payload.color_code,
              color_name: payload.color_name,
              status:
                payload.status ??
                (eventType === "plan_started"
                  ? "running"
                  : eventType === "plan_succeeded"
                  ? "succeeded"
                  : "failed"),
              image_url: payload.image_url,
              elapsed_ms: payload.elapsed_ms,
              error: payload.error,
            });
            return next;
          });
        }
      }
      // 任务级事件：刷一下 DB（拿 cost / 总耗时 / image_prompt_used 等完整字段）
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      if (eventType === "task_finished") {
        es.close();
      }
    });
    return () => es.close();
  }, [taskId, data?.status, queryClient]);

  // 任务一旦不在跑（completed/failed/partial）→ DB 已写全 → 清空 liveResults 避免覆盖
  useEffect(() => {
    if (data && data.status !== "running" && data.status !== "pending" && liveResults.size > 0) {
      setLiveResults(new Map());
    }
  }, [data?.status, liveResults.size]);

  const regenMut = useMutation({
    mutationFn: (planIds: string[]) => regenerateTask(taskId!, { plan_ids: planIds, concurrency: 2 }),
    onSuccess: () => {
      message.success("已启动重生");
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
    },
    onError: (err: any) => message.error("重生失败：" + (err?.response?.data?.detail || err?.message)),
  });

  if (isLoading || !data) return <Spin style={{ margin: 24 }} />;

  // 合并 server.results + 本地 liveResults（后者实时、前者完整）
  // 渲染时统一用 mergedResults，让首张图 30s 后就可见
  const mergedResultsMap = new Map<string, any>();
  for (const r of data.results || []) {
    mergedResultsMap.set(r.plan_id, r);
  }
  for (const [k, v] of liveResults.entries()) {
    // 如果本地已有更新状态（succeeded/failed/running），覆盖 server data
    const existing = mergedResultsMap.get(k);
    mergedResultsMap.set(k, { ...(existing || {}), ...v });
  }
  const mergedResults = Array.from(mergedResultsMap.values());

  // 用 mergedResults 算实时进度 — 让顶部进度条/失败数也跟着每张图刷新
  const liveDone = mergedResults.filter((r) => r.status === "succeeded" || r.status === "failed").length;
  const displayDone = Math.max(liveDone, data.progress_done || 0);
  const percent = data.progress_total > 0 ? Math.round((displayDone / data.progress_total) * 100) : 0;
  const failedPlans = mergedResults.filter((r) => r.status === "failed");
  const planLookup = new Map<string, any>((data.snapshot_plans || []).map((p: any) => [p.方案编号, p]));

  const selectedPlanForModal = imageModalPlanId ? planLookup.get(imageModalPlanId) : null;
  const selectedResultForModal = imageModalPlanId ? mergedResultsMap.get(imageModalPlanId) : null;

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: "0 auto" }}>
      <Space style={{ marginBottom: 16, width: "100%", justifyContent: "space-between" }}>
        <Space>
          <Link to="/tasks"><Button>← 历史</Button></Link>
          <h2 style={{ margin: 0, fontFamily: "ui-monospace, monospace" }}>{data.id}</h2>
          <Tag color={STATUS_COLOR[data.status] || "default"}>{data.status}</Tag>
        </Space>
        <Space>
          <Link to="/tasks/gallery">
            <Button type="primary" ghost>🖼️ 查看全部生图</Button>
          </Link>
        </Space>
      </Space>

      {/* 顶部 banner */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap size="middle">
          <span><strong>{data.trend_name}</strong> × <strong>{data.style_no}</strong></span>
          <span>·</span>
          <span>{displayDone} / {data.progress_total} 张</span>
          {data.total_cost_usd != null && (
            <>
              <span>·</span>
              <span>≈ ${data.total_cost_usd.toFixed(2)}</span>
            </>
          )}
          {data.total_elapsed_ms != null && (
            <>
              <span>·</span>
              <span>{(data.total_elapsed_ms / 1000).toFixed(0)}s</span>
            </>
          )}
          <span>·</span>
          <span style={{ fontSize: 12, color: "#999" }}>源 step2 run: {data.source_step2_run_id}</span>
        </Space>

        {/* Step2 prompt 版本 — 紫色 Tag 表示非默认版本 */}
        <Space size={4} wrap style={{ marginTop: 8, fontSize: 11 }}>
          <span style={{ color: "#999" }}>Step2 Prompt 版本:</span>
          {/* 详情页 banner 展示 v4 的 6 个节点完整版本组 */}
          {["2.1", "2.2", "2.3", "2.4", "2.5", "2.8"].map((nodeId) => {
            const label = data.source_prompt_bundle?.[nodeId];
            const version = extractPromptVersion(label);
            const isNonDefault = label && version !== "current";
            return (
              <Tooltip key={nodeId} title={label || `节点 ${nodeId} 无版本信息`}>
                <Tag
                  color={isNonDefault ? "purple" : "default"}
                  style={{ margin: 0, fontSize: 10, padding: "0 6px", lineHeight: "18px" }}
                >
                  {nodeId} {PROMPT_NODE_LABELS[nodeId]} · <strong>{version}</strong>
                </Tag>
              </Tooltip>
            );
          })}
        </Space>

        <Progress
          percent={percent}
          style={{ marginTop: 8 }}
          status={
            data.status === "failed" ? "exception" :
            data.status === "running" ? "active" :
            data.status === "completed" ? "success" :
            "normal"
          }
        />
      </Card>

      {/* 失败提示 */}
      {failedPlans.length > 0 && (
        <Alert
          type="warning"
          style={{ marginBottom: 16 }}
          message={`有 ${failedPlans.length} 张图失败`}
          description={
            <Button
              size="small"
              loading={regenMut.isPending}
              onClick={() => regenMut.mutate(failedPlans.map((p: any) => p.plan_id))}
            >
              ↻ 一键重生失败的 {failedPlans.length} 张
            </Button>
          }
        />
      )}

      {/* Step 1/2/3 折叠面板 */}
      <Collapse
        defaultActiveKey={["step3"]}
        items={[
          {
            key: "step1",
            label: <Step1Label trendName={data.trend_name} subtopics={data.snapshot_step1?.step1_趋势报告解析?.子主题列表 || []} />,
            children: <Step1Panel snapshot={data.snapshot_step1} />,
          },
          {
            key: "step2",
            label: <Step2Label sourceRunId={data.source_step2_run_id} count={data.snapshot_plans?.length || 0} />,
            children: <Step2Panel meta={data.snapshot_step2_meta} plans={data.snapshot_plans} />,
          },
          {
            key: "step3",
            label: <Step3Label results={mergedResults} total={data.progress_total} />,
            children: (
              <>
                <Space style={{ marginBottom: 12 }}>
                  <span style={{ fontSize: 12, color: "#666" }}>视图：</span>
                  <Segmented
                    size="small"
                    value={step3ViewMode}
                    onChange={(v) => setStep3ViewMode(v as "card" | "tiled")}
                    options={[
                      { label: "卡片", value: "card" },
                      { label: "平铺", value: "tiled" },
                    ]}
                  />
                  <span style={{ fontSize: 11, color: "#999" }}>
                    {step3ViewMode === "card" ? "含色号 / 方案信息 / 重生按钮" : "纯图片墙，点图放大"}
                  </span>
                </Space>
                {step3ViewMode === "card" ? (
                  <Step3Panel
                    snapshotPlans={data.snapshot_plans}
                    results={mergedResults}
                    onClickImage={(planId) => setImageModalPlanId(planId)}
                    onRegenerate={(planId) => regenMut.mutate([planId])}
                    isRegenerating={regenMut.isPending}
                  />
                ) : (
                  <Step3TiledPanel
                    snapshotPlans={data.snapshot_plans}
                    results={mergedResults}
                    onClickImage={(planId) => setImageModalPlanId(planId)}
                  />
                )}
              </>
            ),
          },
        ]}
      />

      {/* 单图模态：大图 + 该图的 prompt */}
      <Modal
        open={!!imageModalPlanId}
        onCancel={() => setImageModalPlanId(null)}
        title={imageModalPlanId}
        width="80vw"
        footer={null}
      >
        {selectedPlanForModal && (
          <ImageModalContent plan={selectedPlanForModal} result={selectedResultForModal} />
        )}
      </Modal>
    </div>
  );
}


// ----- Step 1 ----- //
function Step1Label({ trendName, subtopics }: { trendName: string; subtopics: any[] }) {
  return (
    <Space>
      <strong>Step 1 · 趋势解析</strong>
      <span style={{ color: "#999", fontSize: 12 }}>
        {trendName} · {subtopics.length} 个子主题
      </span>
    </Space>
  );
}

function Step1Panel({ snapshot }: { snapshot: any }) {
  if (!snapshot) return <Empty description="无 step1 快照" />;
  const subtopics = snapshot?.step1_趋势报告解析?.子主题列表 || [];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
      {subtopics.map((s: any) => (
        <Card key={s.编号} size="small" title={<span><Tag color="purple">{s.编号}</Tag>{s.名称}</span>}>
          <div style={{ fontSize: 12, color: "#666" }}>
            <strong>核心图案</strong>（{(s.核心图案列表 || []).length} 个）
            {(s.核心图案列表 || []).slice(0, 2).map((p: any) => (
              <div key={p.图案编号} style={{ marginTop: 4 }}>
                <Tag>{p.图案编号}</Tag> {p.图案内容}
              </div>
            ))}
          </div>
        </Card>
      ))}
    </div>
  );
}


// ----- Step 2 ----- //
function Step2Label({ sourceRunId, count }: { sourceRunId: string; count: number }) {
  return (
    <Space>
      <strong>Step 2 · 方案设计</strong>
      <span style={{ color: "#999", fontSize: 12, fontFamily: "ui-monospace, monospace" }}>
        源 run {sourceRunId} · {count} 方案选中
      </span>
    </Space>
  );
}

function Step2Panel({ meta, plans }: { meta: any; plans: any[] }) {
  return (
    <div>
      {meta?.趋势说明 && (
        <Card size="small" style={{ marginBottom: 12 }}>
          <strong>趋势说明</strong>：{meta.趋势说明}
        </Card>
      )}
      <Collapse
        size="small"
        items={plans.map((p: any) => ({
          key: p.方案编号,
          label: (
            <Space size="small" wrap>
              <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }}>{p.方案编号}</span>
              <Tag color="purple">{p.选用子主题编号}</Tag>
              <Tag>{p.面组合}</Tag>
              <Tag>{p.档组合}</Tag>
              <Tag color={p.适配度 >= 8 ? "green" : "orange"}>{p.适配度}</Tag>
            </Space>
          ),
          children: (
            <div style={{ fontSize: 12 }}>
              <div style={{ marginBottom: 4 }}><strong>方案说明：</strong>{p.方案说明}</div>
              <div style={{ marginBottom: 4 }}><strong>图案内容：</strong>{p.图案内容}</div>
              <div style={{ marginBottom: 4 }}><strong>配色策略：</strong>{p.配色策略}</div>
              <details>
                <summary style={{ cursor: "pointer", margin: "8px 0 4px" }}>图生图 prompt（七段式英文）</summary>
                <pre style={{ background: "#fafafa", padding: 8, fontSize: 11, whiteSpace: "pre-wrap", maxHeight: 300, overflow: "auto" }}>
                  {p.图生图prompt}
                </pre>
              </details>
            </div>
          ),
        }))}
      />
    </div>
  );
}


// ----- Step 3 ----- //
function Step3Label({ results, total }: { results: any[] | null; total: number }) {
  const done = (results || []).filter((r: any) => r.status === "succeeded").length;
  return (
    <Space>
      <strong>Step 3 · 生图结果</strong>
      <span style={{ color: "#999", fontSize: 12 }}>{done} / {total} 成功</span>
    </Space>
  );
}

function Step3Panel({
  snapshotPlans,
  results,
  onClickImage,
  onRegenerate,
  isRegenerating,
}: {
  snapshotPlans: any[];
  results: any[];
  onClickImage: (planId: string) => void;
  onRegenerate: (planId: string) => void;
  isRegenerating: boolean;
}) {
  const resultLookup = new Map(results.map((r) => [r.plan_id, r]));

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 12 }}>
      {snapshotPlans.map((plan: any) => {
        const result = resultLookup.get(plan.方案编号);
        const status = result?.status ?? "pending";
        return (
          <Card
            key={plan.方案编号}
            size="small"
            hoverable={status === "succeeded"}
            onClick={() => status === "succeeded" && onClickImage(plan.方案编号)}
            style={{ cursor: status === "succeeded" ? "pointer" : "default" }}
            bodyStyle={{ padding: 8 }}
          >
            <div style={{
              width: "100%",
              aspectRatio: "1/1.2",
              background: "#fafafa",
              borderRadius: 4,
              marginBottom: 8,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              overflow: "hidden",
            }}>
              {status === "succeeded" && result?.image_url ? (
                <img
                  src={result.image_url}
                  alt={plan.方案编号}
                  style={{ maxWidth: "100%", maxHeight: "100%" }}
                />
              ) : status === "running" ? (
                <Spin tip="生成中..." />
              ) : status === "failed" ? (
                <div style={{ textAlign: "center", color: "#ff4d4f" }}>
                  <div>✗ 失败</div>
                  <div style={{ fontSize: 10, marginTop: 4, padding: 4 }}>{result?.error?.slice(0, 60)}</div>
                </div>
              ) : (
                <div style={{ color: "#999" }}>⏳ 待生成</div>
              )}
            </div>
            <Space direction="vertical" size={0} style={{ width: "100%" }}>
              <Space size={4} wrap>
                <Tag style={{ margin: 0 }}>{plan._色号代码 || plan.色号代码}</Tag>
                <span style={{ fontSize: 11 }}>{plan._营销色名 || plan.营销色名}</span>
                <Tag style={{ margin: 0 }}>{plan._性别定向 || plan.性别定向}</Tag>
              </Space>
              {status === "failed" && (
                <Button
                  size="small"
                  loading={isRegenerating}
                  onClick={(e) => {
                    e.stopPropagation();
                    onRegenerate(plan.方案编号);
                  }}
                >
                  ↻ 重生此张
                </Button>
              )}
              {result?.elapsed_ms != null && (
                <span style={{ fontSize: 11, color: "#999" }}>
                  {(result.elapsed_ms / 1000).toFixed(1)}s
                </span>
              )}
            </Space>
          </Card>
        );
      })}
    </div>
  );
}


// ----- Step 3 平铺视图（纯图片墙） ----- //
function Step3TiledPanel({
  snapshotPlans,
  results,
  onClickImage,
}: {
  snapshotPlans: any[];
  results: any[];
  onClickImage: (planId: string) => void;
}) {
  const resultLookup = new Map(results.map((r) => [r.plan_id, r]));

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))",
        gap: 4,
      }}
    >
      {snapshotPlans.map((plan: any) => {
        const result = resultLookup.get(plan.方案编号);
        const status = result?.status ?? "pending";
        const isSucceeded = status === "succeeded" && result?.image_url;
        return (
          <div
            key={plan.方案编号}
            onClick={() => isSucceeded && onClickImage(plan.方案编号)}
            style={{
              width: "100%",
              aspectRatio: "1/1",
              background: "#fafafa",
              overflow: "hidden",
              cursor: isSucceeded ? "pointer" : "default",
              position: "relative",
              borderRadius: 2,
              border: status === "failed" ? "1px solid #ff4d4f" : "1px solid #eee",
              transition: "transform 0.15s, box-shadow 0.15s",
            }}
            onMouseEnter={(e) => {
              if (isSucceeded) {
                (e.currentTarget as HTMLDivElement).style.transform = "scale(1.02)";
                (e.currentTarget as HTMLDivElement).style.boxShadow =
                  "0 4px 12px rgba(0,0,0,0.12)";
                (e.currentTarget as HTMLDivElement).style.zIndex = "1";
              }
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLDivElement).style.transform = "";
              (e.currentTarget as HTMLDivElement).style.boxShadow = "";
              (e.currentTarget as HTMLDivElement).style.zIndex = "";
            }}
          >
            {isSucceeded ? (
              <img
                src={result.image_url}
                alt={plan.方案编号}
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
              />
            ) : status === "running" ? (
              <div
                style={{
                  width: "100%", height: "100%",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  flexDirection: "column", gap: 6,
                }}
              >
                <Spin size="small" />
                <div style={{ fontSize: 10, color: "#999" }}>生成中</div>
              </div>
            ) : status === "failed" ? (
              <div
                style={{
                  width: "100%", height: "100%",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  flexDirection: "column", gap: 4,
                  color: "#ff4d4f",
                }}
                title={result?.error?.slice(0, 200)}
              >
                <div style={{ fontSize: 20 }}>✗</div>
                <div style={{ fontSize: 10 }}>失败</div>
              </div>
            ) : (
              <div
                style={{
                  width: "100%", height: "100%",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  color: "#bbb",
                }}
              >
                <div style={{ fontSize: 20 }}>⏳</div>
              </div>
            )}
            {/* 悬浮显示色号 + 子主题，hover 时才出 */}
            {isSucceeded && (
              <div
                style={{
                  position: "absolute",
                  bottom: 0, left: 0, right: 0,
                  padding: "4px 6px",
                  background: "linear-gradient(transparent, rgba(0,0,0,0.6))",
                  color: "#fff",
                  fontSize: 10,
                  pointerEvents: "none",
                  opacity: 0,
                  transition: "opacity 0.15s",
                }}
                className="tiled-overlay"
              >
                <div style={{ fontWeight: 600 }}>
                  {plan._色号代码 || plan.色号代码} · {plan._营销色名 || plan.营销色名}
                </div>
                <div style={{ fontSize: 9, opacity: 0.85 }}>
                  {plan.选用子主题编号} {plan.选用子主题名称}
                </div>
              </div>
            )}
          </div>
        );
      })}
      <style>{`
        div[style*="cursor: pointer"]:hover .tiled-overlay { opacity: 1 !important; }
      `}</style>
    </div>
  );
}


// ----- 单图弹窗 ----- //
function ImageModalContent({ plan, result }: { plan: any; result: any }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <div>
        {result?.image_url ? (
          <img src={result.image_url} alt={plan.方案编号} style={{ width: "100%", borderRadius: 4 }} />
        ) : (
          <div style={{ background: "#fafafa", padding: 40, textAlign: "center", color: "#999" }}>
            （图未生成）
          </div>
        )}
      </div>
      <div>
        <Space direction="vertical" style={{ width: "100%" }} size={4}>
          <Space wrap>
            <Tag>{plan._色号代码 || plan.色号代码}</Tag>
            <span>{plan._营销色名 || plan.营销色名}</span>
            <Tag>{plan._性别定向 || plan.性别定向}</Tag>
            <Tag color="purple">{plan.选用子主题编号}</Tag>
          </Space>
          <div><strong>方案说明：</strong>{plan.方案说明}</div>
          <div><strong>图案内容：</strong>{plan.图案内容}</div>
          <div><strong>配色策略：</strong>{plan.配色策略}</div>
          <div>
            <strong>锚点：</strong>
            {(plan.锚点列表 || []).map((a: any, i: number) => (
              <div key={i} style={{ fontSize: 12, marginLeft: 12 }}>
                · {a.面} / {a.档} - {a.位置中文} ({a.尺寸})
              </div>
            ))}
          </div>
          <details>
            <summary style={{ cursor: "pointer", margin: "8px 0 4px" }}>
              图生图 prompt（实际发给 API 的版本）
            </summary>
            <pre style={{
              background: "#1e1e1e",
              color: "#d4d4d4",
              padding: 12,
              fontSize: 11,
              whiteSpace: "pre-wrap",
              borderRadius: 4,
              maxHeight: 400,
              overflow: "auto",
            }}>
              {result?.image_prompt_used || plan.图生图prompt}
            </pre>
          </details>
          {result?.elapsed_ms != null && (
            <div style={{ fontSize: 12, color: "#999", marginTop: 8 }}>
              生成耗时：{(result.elapsed_ms / 1000).toFixed(1)}s
            </div>
          )}
        </Space>
      </div>
    </div>
  );
}
