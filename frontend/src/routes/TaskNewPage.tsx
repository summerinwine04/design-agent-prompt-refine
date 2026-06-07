import { Card, Checkbox, Button, Space, Spin, Empty, Tag, Alert, message, Modal, Collapse } from "antd";
import { useQuery, useMutation } from "@tanstack/react-query";
import { useSearchParams, useNavigate, Link } from "react-router-dom";
import { useEffect, useState } from "react";

import { prepTaskFromRun, createTask } from "../api/client";

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

  // 数据加载完后默认全选所有 plan
  useEffect(() => {
    if (data?.plans?.length) {
      setSelected(new Set(data.plans.map((p: any) => p.方案编号)));
    }
  }, [data]);

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

      {/* 款式 banner */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space>
          <img
            src={data.ref_image_url}
            alt={data.style_no}
            style={{ height: 120, borderRadius: 4, border: "1px solid #eee" }}
            onError={(e: any) => { e.target.style.display = "none"; }}
          />
          <Space direction="vertical" size={0}>
            <div style={{ fontSize: 16, fontWeight: 600 }}>{data.style_no}</div>
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
          {data.plans.map((plan: any) => (
            <PlanCard
              key={plan.方案编号}
              plan={plan}
              checked={selected.has(plan.方案编号)}
              onToggle={() => {
                const ns = new Set(selected);
                if (ns.has(plan.方案编号)) ns.delete(plan.方案编号);
                else ns.add(plan.方案编号);
                setSelected(ns);
              }}
            />
          ))}
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


function PlanCard({ plan, checked, onToggle }: { plan: any; checked: boolean; onToggle: () => void }) {
  const score = plan.适配度;
  const scoreColor = score >= 8 ? "green" : score >= 6 ? "orange" : "red";

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
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <Space>
            <Checkbox checked={checked} onChange={onToggle} onClick={(e) => e.stopPropagation()} />
            <span style={{ fontWeight: 600, fontFamily: "ui-monospace, monospace", fontSize: 12 }}>
              {plan.方案编号}
            </span>
          </Space>
          <Space>
            <Tag>{plan._性别定向 || plan.性别定向}</Tag>
            {plan.是否需要变色 && <Tag color="orange">变色</Tag>}
          </Space>
        </Space>

        <Space size="small" wrap>
          <Tag color="purple">{plan.选用子主题编号} {plan.选用子主题名称}</Tag>
          <Tag>{plan.面组合} / {plan.档组合}</Tag>
          <Tag color={scoreColor}>适配度 {score}</Tag>
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
    </Card>
  );
}
