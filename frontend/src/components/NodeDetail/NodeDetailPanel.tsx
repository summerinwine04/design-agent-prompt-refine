import { Empty, Tabs, Button, Space, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { getNode } from "../../api/client";
import { useRunStore } from "../../store/runStore";
import FaceVisualizer from "../FaceVisualizer/FaceVisualizer";
import PromptDrawer from "../PromptDrawer/PromptDrawer";


/**
 * 从 node_id 推导业务编号（与 TraceTree 的 extractBizNumber 一致）
 *   2_1_001 → 2.1   |   2_4_loop_013 → 2.4.loop   |   2_2_003 → 2.2
 */
function extractBizNumber(nodeId: string): string {
  const parts = nodeId.split("_");
  if (parts.length > 1 && /^\d+$/.test(parts[parts.length - 1])) {
    parts.pop();
  }
  return parts.join(".");
}

// 5 个有 prompt 的节点
const PROMPT_NODES = new Set(["2.1", "2.2", "2.3", "2.4", "2.7"]);

export default function NodeDetailPanel() {
  const { currentRunId, selectedNodeId } = useRunStore();
  const [promptDrawerOpen, setPromptDrawerOpen] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["node", currentRunId, selectedNodeId],
    queryFn: () => getNode(currentRunId!, selectedNodeId!),
    enabled: !!currentRunId && !!selectedNodeId,
  });

  if (!selectedNodeId) {
    return <div style={{ padding: 24 }}><Empty description="选择左侧某个节点" /></div>;
  }
  if (isLoading || !data) return <div style={{ padding: 24 }}>加载中…</div>;

  const bizId = extractBizNumber(data.node_id);
  // 只对 LLM 类型 + 有对应 prompt 文件的节点显示「编辑 prompt」按钮
  const canEditPrompt = data.node_type === "llm" && PROMPT_NODES.has(bizId);

  return (
    <div style={{ padding: 16 }}>
      <Space style={{ marginBottom: 12 }}>
        <Tag color="blue">{data.node_id}</Tag>
        <strong>{data.name}</strong>
        <Tag>{data.node_type}</Tag>
        {data.prompt_version && <Tag color="purple">{data.prompt_version}</Tag>}
        {data.tokens && (
          <span style={{ color: "#999" }}>
            tokens in={data.tokens.input} / out={data.tokens.output}
          </span>
        )}
        {data.elapsed_ms != null && <span style={{ color: "#999" }}>{data.elapsed_ms} ms</span>}
      </Space>

      <Space style={{ marginBottom: 12 }} wrap>
        {canEditPrompt && (
          <Button type="primary" onClick={() => setPromptDrawerOpen(true)}>
            ⚙ 编辑 prompt
          </Button>
        )}
        <Button>⑂ 从此节点 Fork</Button>
        <Button>↻ 仅重跑此节点</Button>
        <Button>📋 加入 fixture</Button>
      </Space>

      <Tabs
        items={[
          {
            key: "prompt",
            // tab 名动态：LLM = 渲染后 Prompt；tool = 输入参数；decision = 决策上下文；loop = 子节点摘要
            label: (
              data.node_type === "llm" ? "渲染后 Prompt" :
              data.node_type === "tool" ? "输入参数" :
              data.node_type === "decision" ? "决策上下文" :
              data.node_type === "loop" ? "子节点摘要" :
              "输入"
            ),
            children: (
              data.node_type === "llm" && data.input?.user_prompt ? (
                <pre style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, whiteSpace: "pre-wrap" }}>
                  {data.input.user_prompt}
                </pre>
              ) : (
                <>
                  {data.node_type !== "llm" && (
                    <div style={{ color: "#999", fontSize: 12, marginBottom: 8 }}>
                      {data.node_type === "tool" && "此节点是 Python tool，没有 LLM prompt，下方是 tool 函数的输入参数。"}
                      {data.node_type === "decision" && "此节点是 Python decision，根据上游审计结果选下游分支。"}
                      {data.node_type === "loop" && "此节点是 loop 容器，本身无输入；具体执行在子节点里。"}
                    </div>
                  )}
                  <pre style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, whiteSpace: "pre-wrap" }}>
                    {JSON.stringify(data.input, null, 2)}
                  </pre>
                </>
              )
            ),
          },
          {
            key: "output",
            // 模型输出 → tool/decision/loop 节点改名为"输出结果"
            label: data.node_type === "llm" ? "模型输出" : "输出结果",
            children: (
              <pre style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, whiteSpace: "pre-wrap" }}>
                {JSON.stringify(data.output, null, 2)}
              </pre>
            ),
          },
          {
            key: "placement",
            label: "Placement 三面图",
            disabled: !data.output?.设计方案,
            children: <FaceVisualizer plans={data.output?.设计方案 ?? []} />,
          },
          {
            key: "raw",
            label: "Raw JSON",
            children: (
              <pre style={{ fontFamily: "ui-monospace, monospace", fontSize: 11, whiteSpace: "pre-wrap" }}>
                {JSON.stringify(data, null, 2)}
              </pre>
            ),
          },
        ]}
      />

      <PromptDrawer
        open={promptDrawerOpen}
        onClose={() => setPromptDrawerOpen(false)}
        nodeBizId={canEditPrompt ? bizId : null}
        nodeName={data.name}
      />
    </div>
  );
}
