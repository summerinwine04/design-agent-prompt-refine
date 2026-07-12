import { Card, Select, Spin, Tag, Space, Button, Empty, Tooltip, Alert, Divider, message } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { listPrompts, getPrompt, listTasks, listRuns } from "../api/client";

/**
 * Prompt 版本页 —— 整组视图（Bundle View）
 *
 * 设计思路：用户调 prompt 时最自然的是"看这个版本组（2.1+2.2+2.3+2.4 各用什么版本）
 * 一起呈现的 4 段 prompt 长什么样"。所以页面：
 *   - 左侧：4 个核心节点 + 2.7 的版本选择器
 *   - 右侧：选中版本组的 4-5 段 prompt 全文（system + user template）
 *   - 顶部：一键预设（最新跑的 task / run / 全部 current）
 */

// v4：6 节点（旧 04_single_color_design / 04b 留作 v3 历史快照）
const PROMPT_NODES = [
  { id: "2.1", name: "款式分析", file: "01_style_analysis.md" },
  { id: "2.2", name: "颜色识别", file: "02_color_recognition.md" },
  { id: "2.3", name: "性别规划", file: "03_gender_planning.md" },
  { id: "2.4", name: "主题选择", file: "04_topic_selection.md" },
  { id: "2.5", name: "单色设计", file: "05_single_color_design.md" },
  { id: "2.8", name: "局部重设计", file: "05b_redesign_constraint.md" },
  // v5 CONVERGE 图（Mode B / Mode C / 图库输入源）专用节点
  { id: "2.4s", name: "单主题选择", file: "04s_single_topic_selection.md" },
  { id: "2.4.5", name: "母图案 Blueprint", file: "045_pattern_blueprint.md" },
  { id: "2.5L", name: "Look 联合设计", file: "05L_look_variant_design.md" },
];

const DEFAULT_BUNDLE = Object.fromEntries(PROMPT_NODES.map((n) => [n.id, "current"]));

function extractVersion(label: string | undefined): string {
  if (!label) return "?";
  const at = label.lastIndexOf("@");
  return at >= 0 ? label.slice(at + 1) : label;
}

export default function PromptsPage() {
  const [bundle, setBundle] = useState<Record<string, string>>(DEFAULT_BUNDLE);

  const { data: promptsMeta, isLoading: metaLoading } = useQuery({
    queryKey: ["prompts"],
    queryFn: listPrompts,
  });

  const setVersion = (nodeId: string, version: string) => {
    setBundle((b) => ({ ...b, [nodeId]: version }));
  };

  if (metaLoading) return <Spin style={{ margin: 24 }} />;

  return (
    <div style={{ padding: 24, display: "grid", gridTemplateColumns: "320px 1fr", gap: 16, height: "calc(100vh - 64px)" }}>
      {/* 左：节点版本选择器 */}
      <div style={{ overflowY: "auto" }}>
        <Card size="small" title="Prompt Bundle (Step 2)">
          <Space direction="vertical" style={{ width: "100%" }} size={12}>
            {PROMPT_NODES.map((node) => {
              const info = (promptsMeta as any)?.[node.id];
              const versions: any[] = info?.versions ?? [{ version: "current" }];
              const selected = bundle[node.id] ?? "current";
              const isNonDefault = selected !== "current";
              return (
                <div key={node.id}>
                  <Space style={{ width: "100%", justifyContent: "space-between" }}>
                    <Space size={4}>
                      <Tag color={isNonDefault ? "purple" : "blue"} style={{ margin: 0 }}>
                        {node.id}
                      </Tag>
                      <span style={{ fontWeight: 500 }}>{node.name}</span>
                    </Space>
                    <span style={{ fontSize: 10, color: "#999" }}>
                      {versions.length} 版本
                    </span>
                  </Space>
                  <Select
                    style={{ width: "100%", marginTop: 4 }}
                    value={selected}
                    onChange={(v) => setVersion(node.id, v)}
                    options={versions.map((v: any) => ({
                      value: v.version,
                      label: v.version,
                    }))}
                    size="small"
                  />
                  <div style={{ fontSize: 10, color: "#999", marginTop: 2, fontFamily: "ui-monospace, monospace" }}>
                    {node.file}
                  </div>
                </div>
              );
            })}
          </Space>

          <Divider style={{ margin: "16px 0 12px" }}>预设</Divider>
          <Space direction="vertical" style={{ width: "100%" }} size={6}>
            <Button size="small" block onClick={() => setBundle(DEFAULT_BUNDLE)}>
              全部用 current
            </Button>
            <LoadFromRunButton onLoad={setBundle} />
            <LoadFromTaskButton onLoad={setBundle} />
          </Space>
        </Card>
      </div>

      {/* 右：整组 prompt 展示 */}
      <div style={{ overflowY: "auto" }}>
        <Card
          size="small"
          title={
            <Space>
              <span>当前选中版本组的 Prompt 全文</span>
              <Tag color="default" style={{ margin: 0 }}>
                {PROMPT_NODES.map((n) => `${n.id}@${bundle[n.id] || "current"}`).join(" · ")}
              </Tag>
            </Space>
          }
        >
          <Space direction="vertical" style={{ width: "100%" }} size="large">
            {PROMPT_NODES.map((node) => (
              <PromptSection
                key={node.id}
                node={node}
                version={bundle[node.id] || "current"}
              />
            ))}
          </Space>
        </Card>
      </div>
    </div>
  );
}


function PromptSection({ node, version }: { node: { id: string; name: string; file: string }; version: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["prompt", node.id, version],
    queryFn: () => getPrompt(node.id, version),
  });

  return (
    <Card
      size="small"
      type="inner"
      title={
        <Space>
          <Tag color="blue" style={{ margin: 0 }}>{node.id}</Tag>
          <span style={{ fontWeight: 600 }}>{node.name}</span>
          <Tag color={version !== "current" ? "purple" : "default"} style={{ margin: 0 }}>
            {version}
          </Tag>
          <span style={{ fontSize: 11, color: "#999", fontFamily: "ui-monospace, monospace" }}>
            {data?.path || node.file}
          </span>
        </Space>
      }
    >
      {isLoading ? (
        <Spin />
      ) : error ? (
        <Alert type="error" message={`加载失败：${(error as any).message || "未知错误"}`} />
      ) : !data ? (
        <Empty description="无内容" />
      ) : (
        <Space direction="vertical" style={{ width: "100%" }} size={12}>
          <Section title="SYSTEM PROMPT" content={data.system_prompt} />
          <Section title={`USER PROMPT TEMPLATE （${(data.placeholders || []).length} 占位符）`}
                   content={data.user_template} />
          {data.placeholders?.length > 0 && (
            <div style={{ fontSize: 11, color: "#666" }}>
              <span style={{ color: "#999" }}>占位符：</span>
              {data.placeholders.map((p: string) => (
                <Tag key={p} style={{ margin: "2px 4px 2px 0", fontFamily: "ui-monospace, monospace", fontSize: 10 }}>
                  {p}
                </Tag>
              ))}
            </div>
          )}
        </Space>
      )}
    </Card>
  );
}


function Section({ title, content }: { title: string; content: string }) {
  if (!content) return null;
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, color: "#666", marginBottom: 4 }}>{title}</div>
      <pre style={{
        background: "#fafafa",
        border: "1px solid #eee",
        borderRadius: 4,
        padding: 12,
        fontSize: 12,
        fontFamily: "ui-monospace, monospace",
        whiteSpace: "pre-wrap",
        margin: 0,
        maxHeight: 360,
        overflow: "auto",
      }}>
        {content}
      </pre>
    </div>
  );
}


// ============================================================================
// 快捷预设：从某 run 加载 bundle
// ============================================================================

function LoadFromRunButton({ onLoad }: { onLoad: (bundle: Record<string, string>) => void }) {
  const { data: runs } = useQuery({ queryKey: ["runs-history"], queryFn: () => listRuns() });
  const realRuns = useMemo(
    () => (runs || []).filter((r: any) => r.total_tokens_in && r.total_tokens_in > 0).slice(0, 20),
    [runs],
  );

  if (!realRuns.length) {
    return <Button size="small" block disabled>暂无真跑 Run</Button>;
  }

  return (
    <Select
      placeholder="从某 step2 run 加载版本组"
      style={{ width: "100%" }}
      size="small"
      allowClear
      onChange={(runId) => {
        if (!runId) return;
        const r = (runs as any[]).find((x) => x.id === runId);
        if (!r?.prompt_bundle) {
          message.warning("该 run 无 prompt_bundle 信息");
          return;
        }
        // r.prompt_bundle 是 JSON string，从后端 list_runs 返回
        try {
          const b = typeof r.prompt_bundle === "string" ? JSON.parse(r.prompt_bundle) : r.prompt_bundle;
          const newBundle: Record<string, string> = {};
          for (const n of PROMPT_NODES) {
            newBundle[n.id] = extractVersion(b[n.id]);
          }
          onLoad(newBundle);
          message.success(`已加载 ${r.style_no}@${runId.slice(-8)} 的版本组`);
        } catch (err) {
          message.error("解析 prompt_bundle 失败");
        }
      }}
      options={realRuns.map((r: any) => ({
        value: r.id,
        label: `${r.style_no} · ${r.id.slice(-12)}`,
      }))}
    />
  );
}


function LoadFromTaskButton({ onLoad }: { onLoad: (bundle: Record<string, string>) => void }) {
  const { data: tasks } = useQuery({ queryKey: ["tasks"], queryFn: listTasks });

  if (!tasks?.length) {
    return <Button size="small" block disabled>暂无生图任务</Button>;
  }

  return (
    <Select
      placeholder="从某生图任务加载版本组"
      style={{ width: "100%" }}
      size="small"
      allowClear
      onChange={(taskId) => {
        if (!taskId) return;
        const t = (tasks as any[]).find((x) => x.id === taskId);
        if (!t?.source_prompt_bundle) {
          message.warning("该 task 无 prompt_bundle 信息");
          return;
        }
        const newBundle: Record<string, string> = {};
        for (const n of PROMPT_NODES) {
          newBundle[n.id] = extractVersion(t.source_prompt_bundle[n.id]);
        }
        onLoad(newBundle);
        message.success(`已加载 task ${taskId.slice(-8)} 的版本组`);
      }}
      options={tasks.map((t: any) => ({
        value: t.id,
        label: `${t.style_no} · ${t.id.slice(-12)}`,
      }))}
    />
  );
}
