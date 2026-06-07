import { Tag, Space, Statistic, Button } from "antd";
import { useMemo } from "react";
import { Link } from "react-router-dom";

import { useRunStore } from "../../store/runStore";

/**
 * 当前 run 顶部状态条：run_id、status、进度、累计 token、耗时
 * 实时从 trace 事件流派生
 *
 * 注意：node_completed / node_failed 事件不带 node_type，
 * 必须先从 node_started 学到每个 node_id 的类型，再用 nodeId→type 反查
 * 来正确排除 loop 节点（避免 completed > started 的反直觉）
 */
export default function RunHeader() {
  const { currentRunId, status, trace } = useRunStore();

  const stats = useMemo(() => {
    const nodeTypes = new Map<string, string>();
    const completed = new Set<string>();
    const failed = new Set<string>();
    let tokensIn = 0;
    let tokensOut = 0;
    let elapsedMs = 0;

    // 一遍扫描，分类记录
    for (const ev of trace) {
      if (!ev.node_id) continue;
      if (ev.event === "node_started") {
        nodeTypes.set(ev.node_id, ev.node_type || "");
      } else if (ev.event === "node_completed") {
        completed.add(ev.node_id);
        if (ev.tokens) {
          tokensIn += ev.tokens.input || 0;
          tokensOut += ev.tokens.output || 0;
        }
        // loop 节点的 elapsed 是子节点之和，不重复累加
        if (ev.elapsed_ms && nodeTypes.get(ev.node_id) !== "loop") {
          elapsedMs += ev.elapsed_ms;
        }
      } else if (ev.event === "node_failed") {
        failed.add(ev.node_id);
      }
    }

    // 只统计非 loop 节点
    const nonLoopIds = Array.from(nodeTypes.keys()).filter(
      (id) => nodeTypes.get(id) !== "loop",
    );
    const nodesStarted = nonLoopIds.length;
    const nodesCompleted = nonLoopIds.filter((id) => completed.has(id)).length;
    const nodesFailed = nonLoopIds.filter((id) => failed.has(id)).length;

    return { nodesStarted, nodesCompleted, nodesFailed, tokensIn, tokensOut, elapsedMs };
  }, [trace]);

  if (!currentRunId) {
    return (
      <div style={{ padding: "8px 16px", background: "#fafafa", borderBottom: "1px solid #eee", color: "#999", fontSize: 12 }}>
        尚未启动 run。左侧填表点「跑」开始。
      </div>
    );
  }

  // 仅"完整跑完"的状态显示生图入口
  const canStartGeneration =
    !!currentRunId &&
    (status === "succeeded" || status === "succeeded_with_audit_warnings");

  return (
    <div style={{ padding: "8px 16px", background: "#fff", borderBottom: "1px solid #eee" }}>
      <Space size="large" wrap style={{ width: "100%", justifyContent: "space-between" }}>
        <Space size="large" wrap>
          <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, color: "#666" }}>
            {currentRunId}
          </span>
          <Tag color={STATUS_COLOR[status]}>{status}</Tag>
          <Statistic
            title="已完成节点"
            value={stats.nodesCompleted}
            suffix={`/ ${stats.nodesStarted}`}
            valueStyle={{ fontSize: 14 }}
          />
          <Statistic
            title="累计 token"
            value={stats.tokensIn + stats.tokensOut}
            formatter={(v) => formatNumber(Number(v))}
            valueStyle={{ fontSize: 14 }}
          />
          <Statistic
            title="累计耗时"
            value={(stats.elapsedMs / 1000).toFixed(1)}
            suffix="s"
            valueStyle={{ fontSize: 14 }}
          />
          {stats.nodesFailed > 0 && (
            <Tag color="red">{stats.nodesFailed} 节点失败</Tag>
          )}
        </Space>

        {canStartGeneration && (
          <Link to={`/tasks/new?run=${currentRunId}`}>
            <Button type="primary">→ 应用方案生图</Button>
          </Link>
        )}
      </Space>
    </div>
  );
}

const STATUS_COLOR: Record<string, string> = {
  idle: "default",
  running: "processing",
  succeeded: "success",
  succeeded_with_audit_warnings: "warning",
  failed: "error",
};

function formatNumber(n: number): string {
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
