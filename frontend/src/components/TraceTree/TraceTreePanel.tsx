import { Tree, Tag, Empty } from "antd";
import type { DataNode } from "antd/es/tree";

import { useRunStore } from "../../store/runStore";

const NODE_ICONS: Record<string, string> = {
  llm: "🧠",
  tool: "🔧",
  decision: "🔀",
  loop: "🔁",
};

const STATUS_COLORS: Record<string, string> = {
  pending: "default",
  running: "blue",
  succeeded: "green",
  failed: "red",
};


/**
 * 从 node_id 提取业务编号
 *   2_1_001        → 2.1
 *   2_2_003        → 2.2
 *   2_2_loop_002   → 2.2.loop
 *   2_4_014        → 2.4
 *   2_4_loop_013   → 2.4.loop
 *
 * 规则：去掉末尾的 _数字 序号，剩下的 _ 替换为 .
 */
function extractBizNumber(nodeId: string): string {
  const parts = nodeId.split("_");
  // 末尾是纯数字 → 去掉（那是 trace 的 counter）
  if (parts.length > 1 && /^\d+$/.test(parts[parts.length - 1])) {
    parts.pop();
  }
  return parts.join(".");
}


export default function TraceTreePanel() {
  const { trace, selectedNodeId, selectNode, currentRunId } = useRunStore();

  if (!currentRunId || trace.length === 0) {
    return (
      <div style={{ padding: 24 }}>
        <Empty description="尚未运行；从左侧 Run 按钮开始" />
      </div>
    );
  }

  // 把 trace 事件流折叠成树（按 node_id + parent_id）
  const nodeMap = new Map<string, any>();
  const status = new Map<string, string>();
  const tokens = new Map<string, { input?: number; output?: number }>();
  const elapsed = new Map<string, number>();

  for (const ev of trace) {
    if (!ev.node_id) continue;
    if (ev.event === "node_started") {
      nodeMap.set(ev.node_id, {
        node_id: ev.node_id,
        parent_id: ev.parent_id,
        node_type: ev.node_type,
        name: ev.name,
        prompt_version: ev.prompt_version,
        children: [],
      });
      status.set(ev.node_id, "running");
    }
    if (ev.event === "node_completed") status.set(ev.node_id, "succeeded");
    if (ev.event === "node_failed") status.set(ev.node_id, "failed");
    if (ev.tokens) tokens.set(ev.node_id, ev.tokens);
    if (ev.elapsed_ms != null) elapsed.set(ev.node_id, ev.elapsed_ms);
  }

  // 构造 antd Tree dataSource
  const roots: DataNode[] = [];
  const byId = new Map<string, DataNode>();
  for (const n of nodeMap.values()) {
    const bizNum = extractBizNumber(n.node_id);
    byId.set(n.node_id, {
      key: n.node_id,
      title: (
        <span>
          <span style={{ marginRight: 6 }}>{NODE_ICONS[n.node_type] ?? "•"}</span>
          {/* 业务编号——用浅色等宽字凸显层级 */}
          <span
            style={{
              fontFamily: "ui-monospace, monospace",
              fontSize: 12,
              color: "#999",
              marginRight: 6,
            }}
          >
            {bizNum}
          </span>
          {n.name}
          <Tag color={STATUS_COLORS[status.get(n.node_id) ?? "pending"]} style={{ marginLeft: 8 }}>
            {status.get(n.node_id)}
          </Tag>
          {elapsed.get(n.node_id) != null && (
            <span style={{ marginLeft: 8, color: "#999", fontSize: 12 }}>
              {elapsed.get(n.node_id)} ms
            </span>
          )}
          {tokens.get(n.node_id) && (tokens.get(n.node_id)!.input || tokens.get(n.node_id)!.output) ? (
            <span style={{ marginLeft: 8, color: "#999", fontSize: 12 }}>
              {(tokens.get(n.node_id)!.input || 0) + (tokens.get(n.node_id)!.output || 0)} tok
            </span>
          ) : null}
        </span>
      ),
      children: [],
    });
  }
  for (const n of nodeMap.values()) {
    const node = byId.get(n.node_id)!;
    if (n.parent_id && byId.has(n.parent_id)) {
      (byId.get(n.parent_id)!.children as DataNode[]).push(node);
    } else {
      roots.push(node);
    }
  }

  return (
    <div style={{ padding: 16 }}>
      <div style={{ marginBottom: 8, fontWeight: 600, fontFamily: "ui-monospace, monospace", fontSize: 12 }}>
        Run: {currentRunId}
      </div>
      <Tree
        treeData={roots}
        defaultExpandAll
        selectedKeys={selectedNodeId ? [selectedNodeId] : []}
        onSelect={(keys) => keys[0] && selectNode(String(keys[0]))}
      />
    </div>
  );
}
