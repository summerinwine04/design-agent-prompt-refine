import { Card, Checkbox, Button, Space, Spin, Empty, Tag, Alert, message, Modal, Collapse } from "antd";
import { useQuery, useMutation } from "@tanstack/react-query";
import { useSearchParams, useNavigate, Link } from "react-router-dom";
import { useEffect, useState } from "react";

import { prepTaskFromRun, createTask, getStyle } from "../api/client";

/**
 * 新建生图任务
 *
 * URL 参数：?run=<source_step2_run_id>
 * 工作台 RunHeader「→ 应用方案生图」按钮跳转到这里
 *
 * 页面流：选方案（默认全选） → 点提交 → 创建 task → 跳详情页
 */
export default function TaskNewPage() {
  const [searchParams] = useSearchParams();
  const sourceRunId = searchParams.get("run");
  const navigate = useNavigate();
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const { data, isLoading, error } = useQuery({
    queryKey: ["prep-from-run", sourceRunId],
    queryFn: () => prepTaskFromRun(sourceRunId!),
    enabled: !!sourceRunId,
  });

  // 拉款号详情拿色号图 URL（给每张方案卡显示对应色号实物图）
  // 成套 run（design_mode 收敛）的 style_no 是组合串（"款A+款B"）查不到——
  // 后端已在每个 plan 上直发 _color_image_url / _ref_image_url，无需再查
  const isConverge = !!data?.styles?.length;
  const { data: styleDetail } = useQuery({
    queryKey: ["style-detail", data?.style_no],
    queryFn: () => getStyle(data!.style_no),
    enabled: !!data?.style_no && !isConverge,
  });

  // (色号代码, 营销色名) → 色号图 URL 映射
  const colorImageMap = new Map<string, string>();
  for (const c of styleDetail?.colors || []) {
    if (c.url) colorImageMap.set(`${c.code}|${c.name}`, c.url);
  }

  // 数据加载完后默认全选所有 plan
  useEffect(() => {
    if (data?.plans?.length) {
      setSelected(new Set(data.plans.map((p: any) => p.方案编号)));
    }
  }, [data]);

  // 成套模式也走单任务：后端 runner 已支持按 role 混装（逐方案取各自款图）
  const createMut = useMutation({
    mutationFn: () => createTask({
      source_step2_run_id: sourceRunId!,
      selected_plan_ids: Array.from(selected),
      concurrency: 2,
    }),
    onSuccess: (task) => {
      message.success(`已启动生图任务 ${task.id}`);
      navigate(`/tasks/${task.id}`);
    },
    onError: (err: any) => {
      Modal.error({
        title: "创建任务失败",
        content: err?.response?.data?.detail || err?.message || String(err),
      });
    },
  });

  if (!sourceRunId) {
    return <Empty description="缺少 run 参数。请从工作台 RunHeader 进入。" style={{ marginTop: 80 }} />;
  }
  if (isLoading) return <Spin style={{ margin: 24 }} />;
  if (error || !data) return <Alert type="error" message="加载源 run 失败" style={{ margin: 24 }} />;

  const totalCost = selected.size * 0.06;       // 估算

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: "0 auto" }}>
      <Space style={{ marginBottom: 16, width: "100%", justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>新建生图任务</h2>
        <Link to="/"><Button>← 返回工作台</Button></Link>
      </Space>

      <Alert
        message={
          <Space wrap>
            <span>源 step2 run：<code>{sourceRunId}</code></span>
            <span>·</span>
            <span>{data.trend_name} × {data.style_no}</span>
            <span>·</span>
            <span>共 {data.plans.length} 个方案</span>
          </Space>
        }
        type="info"
        style={{ marginBottom: 16 }}
      />

      {/* 款式 banner：成套 run 展示上下装两张款图，单款 run 保持原样 */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space align="start">
          {isConverge ? (
            data.styles.map((s: any) => (
              <div key={s.role} style={{ textAlign: "center" }}>
                <img
                  src={s.ref_image_url}
                  alt={s.style_no}
                  style={{ height: 120, borderRadius: 4, border: "1px solid #eee" }}
                  onError={(e: any) => { e.target.style.display = "none"; }}
                />
                <div style={{ fontSize: 11, color: "#666", marginTop: 2 }}>
                  {s.role === "bottom" ? "👖" : "👕"} {s.style_no}
                </div>
              </div>
            ))
          ) : (
            <img
              src={data.ref_image_url}
              alt={data.style_no}
              style={{ height: 120, borderRadius: 4, border: "1px solid #eee" }}
              onError={(e: any) => { e.target.style.display = "none"; }}
            />
          )}
          <Space direction="vertical" size={0}>
            <div style={{ fontSize: 16, fontWeight: 600 }}>{data.style_no}</div>
            {isConverge && (
              <div style={{ fontSize: 12, color: "#7c3aed" }}>
                👕👖 上下装成套 · 同一任务混装，上下装方案各用自己的款图生成
              </div>
            )}
            {data.meta?.款式分析?.结构 && (
              <div style={{ fontSize: 12, color: "#666" }}>{data.meta.款式分析.结构}</div>
            )}
            {data.meta?.趋势说明 && (
              <div style={{ fontSize: 12, color: "#666", maxWidth: 600 }}>
                趋势说明：{data.meta.趋势说明}
              </div>
            )}
          </Space>
        </Space>
      </Card>

      {/* 方案多选 grid */}
      <Card
        size="small"
        title={
          <Space>
            <span>选择要进入生图的方案</span>
            <Tag color="blue">{selected.size} / {data.plans.length} 已选</Tag>
            <Button size="small" onClick={() => setSelected(new Set(data.plans.map((p: any) => p.方案编号)))}>
              全选
            </Button>
            <Button size="small" onClick={() => setSelected(new Set())}>
              反选/清空
            </Button>
          </Space>
        }
        extra={
          <span style={{ color: "#999", fontSize: 12 }}>预估成本 ≈ ${totalCost.toFixed(2)} USD</span>
        }
      >
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          {data.plans.map((plan: any) => {
            const code = plan._色号代码 || plan.色号代码;
            const name = plan._营销色名 || plan.营销色名;
            // 成套 run：后端直发 URL；单款 run：走 getStyle 的映射
            const colorImageUrl = plan._color_image_url
              || (code && name ? colorImageMap.get(`${code}|${name}`) : undefined);
            return (
              <PlanCard
                key={plan.方案编号}
                plan={plan}
                colorImageUrl={colorImageUrl}
                checked={selected.has(plan.方案编号)}
                onToggle={() => {
                  const ns = new Set(selected);
                  if (ns.has(plan.方案编号)) ns.delete(plan.方案编号);
                  else ns.add(plan.方案编号);
                  setSelected(ns);
                }}
              />
            );
          })}
        </div>
      </Card>

      {/* 底部提交栏 */}
      <div style={{ position: "sticky", bottom: 0, background: "#fff", padding: 16, borderTop: "1px solid #eee", marginTop: 16 }}>
        <Space style={{ width: "100%", justifyContent: "flex-end" }}>
          <span style={{ color: "#999" }}>
            将启动 {selected.size} 张图的生成（gpt-image-2，约 30-60 秒/张，并发 2）
          </span>
          <Button
            type="primary"
            disabled={selected.size === 0}
            loading={createMut.isPending}
            onClick={() => createMut.mutate()}
          >
            🚀 启动生图 ({selected.size})
          </Button>
        </Space>
      </div>
    </div>
  );
}


function PlanCard({
  plan,
  checked,
  onToggle,
  colorImageUrl,
}: {
  plan: any;
  checked: boolean;
  onToggle: () => void;
  colorImageUrl?: string;
}) {
  // v4 提示：早期 v4 跑的 task 可能缺"适配度"字段（prompt schema 当时没加），fallback 显示 "—"
  const score = plan.适配度;
  const hasScore = typeof score === "number";
  const scoreColor = !hasScore ? "default" : score >= 8 ? "green" : score >= 6 ? "orange" : "red";
  const colorCode = plan._色号代码 || plan.色号代码;
  const colorName = plan._营销色名 || plan.营销色名;

  return (
    <Card
      size="small"
      style={{
        borderColor: checked ? "#1677ff" : undefined,
        background: checked ? "#e6f4ff" : undefined,
        cursor: "pointer",
      }}
      onClick={onToggle}
      bodyStyle={{ padding: 12 }}
    >
      <Space style={{ width: "100%", alignItems: "flex-start" }} size={10}>
        {/* 左侧：色号缩略图 */}
        <div style={{ flexShrink: 0, width: 80 }}>
          {colorImageUrl ? (
            <img
              src={colorImageUrl}
              alt={`${colorCode} ${colorName}`}
              style={{
                width: 80,
                height: 80,
                objectFit: "cover",
                borderRadius: 4,
                border: "1px solid #eee",
                background: "#fff",
                display: "block",
              }}
              onError={(e: any) => { e.target.style.opacity = "0.3"; }}
            />
          ) : (
            <div style={{
              width: 80, height: 80, background: "#fafafa", borderRadius: 4,
              display: "flex", alignItems: "center", justifyContent: "center",
              color: "#bbb", fontSize: 10, textAlign: "center", padding: 4,
              border: "1px dashed #ddd",
            }}>
              {colorCode || "无色号图"}
            </div>
          )}
          <div style={{ fontSize: 10, color: "#888", marginTop: 4, textAlign: "center", lineHeight: 1.3 }}>
            <div style={{ fontFamily: "ui-monospace, monospace" }}>{colorCode}</div>
            <div style={{ color: "#444", fontWeight: 500 }}>{colorName}</div>
          </div>
        </div>

        {/* 右侧：方案信息（保持原文字布局）*/}
        <Space direction="vertical" size={6} style={{ flex: 1, minWidth: 0 }}>
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <Space>
            <Checkbox checked={checked} onChange={onToggle} onClick={(e) => e.stopPropagation()} />
            <span style={{ fontWeight: 600, fontFamily: "ui-monospace, monospace", fontSize: 12 }}>
              {plan.方案编号}
            </span>
          </Space>
          <Space>
            {plan.role && (
              <Tag color={plan.role === "bottom" ? "orange" : "cyan"}>
                {plan.role === "bottom" ? "👖" : "👕"} {plan._款号 || plan.role}
              </Tag>
            )}
            <Tag>{plan._性别定向 || plan.性别定向}</Tag>
            {plan.是否需要变色 && <Tag color="orange">变色</Tag>}
          </Space>
        </Space>

        <Space size="small" wrap>
          <Tag color="purple">{plan.选用子主题编号} {plan.选用子主题名称}</Tag>
          <Tag>{plan.面组合} / {plan.档组合}</Tag>
          <Tag color={scoreColor}>适配度 {hasScore ? score : "—"}</Tag>
        </Space>

        <div style={{ fontSize: 12, color: "#444" }}>
          <strong>方案说明：</strong>{plan.方案说明}
        </div>

        <Collapse
          size="small"
          ghost
          onClick={(e) => e.stopPropagation()}
          items={[
            {
              key: "prompt",
              label: <span style={{ fontSize: 12 }}>查看图生图 prompt（七段式英文）</span>,
              children: (
                <pre style={{
                  fontFamily: "ui-monospace, monospace",
                  fontSize: 11,
                  whiteSpace: "pre-wrap",
                  background: "#fafafa",
                  padding: 8,
                  borderRadius: 4,
                  maxHeight: 300,
                  overflow: "auto",
                }}>
                  {plan.图生图prompt}
                </pre>
              ),
            },
            {
              key: "anchors",
              label: <span style={{ fontSize: 12 }}>查看锚点列表（{(plan.锚点列表 || []).length} 个）</span>,
              children: (
                <div style={{ fontSize: 12 }}>
                  {(plan.锚点列表 || []).map((a: any, i: number) => (
                    <div key={i} style={{ marginBottom: 4 }}>
                      <Tag>{a.面} / {a.档}</Tag>
                      {a.位置中文} · {a.尺寸}
                    </div>
                  ))}
                </div>
              ),
            },
          ]}
        />
        </Space>
      </Space>
    </Card>
  );
}
