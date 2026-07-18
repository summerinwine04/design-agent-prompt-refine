import { Empty, Tabs, Button, Space, Tag, Modal, message } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { getNode, recognizeColor, redesignSingleColor } from "../../api/client";
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

// 有 prompt 的节点：v4 的 6 个 + v5 CONVERGE 图（强单主题/成套）的 3 个，都能编辑 prompt + fork
const PROMPT_NODES = new Set(["2.1", "2.2", "2.3", "2.4", "2.5", "2.8", "2.4s", "2.4.5", "2.5L", "2.4p"]);

export default function NodeDetailPanel() {
  const { currentRunId, selectedNodeId } = useRunStore();
  const loadRun = useRunStore((s) => s.loadRun);
  const forkFromCurrent = useRunStore((s) => s.forkFromCurrent);
  const promptOverrides = useRunStore((s) => s.promptOverrides);
  const [promptDrawerOpen, setPromptDrawerOpen] = useState(false);
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["node", currentRunId, selectedNodeId],
    queryFn: () => getNode(currentRunId!, selectedNodeId!),
    enabled: !!currentRunId && !!selectedNodeId,
  });

  // 单色重设计 mutation（重识别后的级联）
  const redesignMut = useMutation({
    mutationFn: (payload: { color_code: string; color_name: string }) =>
      redesignSingleColor(currentRunId!, payload),
    onSuccess: async (result) => {
      Modal.success({
        title: `单色 2.4 方案重设计成功：${result.color_code}-${result.color_name}`,
        content: (
          <div style={{ fontSize: 12 }}>
            <p>新生成方案数：{result.design_count}</p>
            <p style={{ color: "#999", marginTop: 8 }}>
              tokens in/out: {result.tokens_in}/{result.tokens_out} · {result.elapsed_sec?.toFixed(1)}s
            </p>
            <p style={{ color: "#999", fontSize: 11 }}>
              其他 8 个色号方案保持不变。审计未重新跑——如要重新审计/触发 2.7 重设计，请走 Fork。
            </p>
          </div>
        ),
      });
      queryClient.invalidateQueries({ queryKey: ["node", currentRunId, selectedNodeId] });
      if (currentRunId) await loadRun(currentRunId);
    },
    onError: (err: any) => {
      message.error("重设计失败：" + (err?.response?.data?.detail || err?.message));
    },
  });

  // 单色重识别 mutation
  const recognizeMut = useMutation({
    mutationFn: (payload: { color_code: string; color_name: string }) =>
      recognizeColor(currentRunId!, payload),
    onSuccess: async (result) => {
      Modal.confirm({
        title: `重新识别成功：${result.color_code}-${result.color_name}`,
        icon: null,
        content: (
          <div style={{ fontSize: 12 }}>
            <p>新的识别底色：{result.new_output?.识别底色?.文字描述 || "—"}</p>
            <p>是否需要变色：{result.new_output?.是否需要变色 ? "是" : "否"}</p>
            <p style={{ color: "#999", marginTop: 8 }}>
              tokens in/out: {result.tokens_in}/{result.tokens_out} · {result.elapsed_sec?.toFixed(1)}s
            </p>
            <p style={{ marginTop: 12, padding: 8, background: "#fffbe6", borderRadius: 4, fontSize: 11 }}>
              ℹ <strong>下游 2.4 方案设计</strong>没有自动更新——它仍然是基于旧识别色出的。<br />
              要让 2.4 方案也跟着新识别色重做（仅这一个色号、不动其他 8 色），点下方「继续重设计 2.4」。
            </p>
          </div>
        ),
        okText: "继续重设计该色号 2.4 →",
        cancelText: "暂不更新下游",
        okButtonProps: { type: "primary" },
        width: 540,
        onOk: () => {
          // 触发 2.4 单色重设计
          redesignMut.mutate({
            color_code: result.color_code,
            color_name: result.color_name,
          });
        },
      });
      queryClient.invalidateQueries({ queryKey: ["node", currentRunId, selectedNodeId] });
      if (currentRunId) await loadRun(currentRunId);
    },
    onError: (err: any) => {
      message.error("重新识别失败：" + (err?.response?.data?.detail || err?.message));
    },
  });

  if (!selectedNodeId) {
    return <div style={{ padding: 24 }}><Empty description="选择左侧某个节点" /></div>;
  }
  if (isLoading || !data) return <div style={{ padding: 24 }}>加载中…</div>;

  const bizId = extractBizNumber(data.node_id);
  // 只对 LLM 类型 + 有对应 prompt 文件的节点显示「编辑 prompt」按钮
  const canEditPrompt = data.node_type === "llm" && PROMPT_NODES.has(bizId);

  // 取 fork 入口的业务编号——loop 节点去掉 .loop 后缀（"2.2.loop" → "2.2"、"2.5L.loop" → "2.5L"）
  // 注意不能按段截断："2.4.5" 是合法三段节点 id
  const forkBizId = bizId.endsWith(".loop") ? bizId.slice(0, -".loop".length) : bizId;
  const canFork = PROMPT_NODES.has(forkBizId) && !!currentRunId;

  const handleFork = () => {
    if (!currentRunId) return;
    Modal.confirm({
      title: `⑂ 从 ${forkBizId} 开始 Fork`,
      content: (
        <div>
          <p>将基于当前 run <code>{currentRunId}</code> 创建新 run：</p>
          <ul style={{ marginLeft: 16, fontSize: 12 }}>
            <li>{forkBizId} 上游节点 → ⚡ 复用缓存（秒级）</li>
            <li>{forkBizId} 及下游节点 → 真跑（烧 token）</li>
          </ul>
          {Object.keys(promptOverrides).length > 0 && (
            <p style={{ fontSize: 12, color: "#666", marginTop: 8 }}>
              当前 prompt overrides 将一同应用：
              {Object.entries(promptOverrides).map(([k, v]) => `${k}=${v}`).join(", ")}
            </p>
          )}
        </div>
      ),
      okText: "启动 Fork",
      cancelText: "取消",
      onOk: async () => {
        try {
          await forkFromCurrent(`node_${forkBizId}`, promptOverrides);
          message.success("Fork 已启动 —— 可看 trace 树实时进度");
        } catch (err: any) {
          message.error("Fork 失败：" + (err?.response?.data?.detail || err?.message));
        }
      },
    });
  };

  // 判断是否是颜色识别节点（2.2 单色或 2.2.loop 都可能）+ 是否失败/可重识别
  const isColorNode = bizId === "2.2" && data.node_type === "llm";
  // 从 node.name 兜底提取色号 + 色名（形如 "单色设计·DSC09156·淡卡其" / "颜色识别·DSC09156·淡卡其"）
  // 老 run 的 cache input 可能缺 营销色名 字段，这是 fallback 路径
  const nameParts = (data.name || "").split("·");
  const codeFromName = nameParts.length >= 3 ? nameParts[1] : null;
  const nameFromName = nameParts.length >= 3 ? nameParts.slice(2).join("·") : null;
  const colorCode = data.input?.色号代码 || data.output?.色号代码 || codeFromName;
  const colorName = data.input?.营销色名 || data.output?.营销色名 || nameFromName;
  const isRecognitionFailed = data.output?._recognition_failed === true || (isColorNode && data.status === "failed");
  const canRecognize = isColorNode && !!colorCode && !!colorName;

  // 2.5 单色设计节点（v4 起从 2.4 改到 2.5）+ 失败状态 → 显示「重新生成此色号方案」按钮
  // 失败两种信号：(a) trace status === "failed"（硬失败：LLM 抛异常）
  //                (b) output._design_failed === true（向后兼容软失败标记）
  const isDesignNode = bizId === "2.5" && data.node_type === "llm";
  const isDesignFailed = isDesignNode && (data.status === "failed" || data.output?._design_failed === true);
  const canRedesign = isDesignNode && !!colorCode && !!colorName;

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
        {canRecognize && (
          <Button
            type={isRecognitionFailed ? "primary" : "default"}
            danger={isRecognitionFailed}
            loading={recognizeMut.isPending}
            onClick={() => recognizeMut.mutate({ color_code: colorCode, color_name: colorName })}
          >
            ↻ 重新识别此色号{isRecognitionFailed ? "（修复失败）" : ""}
          </Button>
        )}
        {canRedesign && isDesignFailed && (
          <Button
            type="primary"
            danger
            loading={redesignMut.isPending}
            onClick={() => Modal.confirm({
              title: `↻ 重新生成 ${colorCode}-${colorName} 的设计方案`,
              icon: null,
              content: (
                <div style={{ fontSize: 12 }}>
                  <p>仅对这一个色号重跑 2.4 LLM 调用，<strong>其他色号方案保持不变</strong>。</p>
                  <p style={{ color: "#999", marginTop: 8 }}>
                    会复用原 prompt（含累积状态、最新识别色信息），不会重新跑审计。
                  </p>
                  <p style={{ marginTop: 12, padding: 8, background: "#fff1f0", borderRadius: 4, fontSize: 11 }}>
                    ⚠ 烧 token + 等约 30-60 秒；如果上游 2.2 颜色识别有问题，请先点「↻ 重新识别」修复。
                  </p>
                </div>
              ),
              okText: "重新生成",
              okType: "primary",
              cancelText: "取消",
              width: 480,
              onOk: () => redesignMut.mutate({ color_code: colorCode, color_name: colorName }),
            })}
          >
            ↻ 重新生成此色号方案（修复失败）
          </Button>
        )}
        <Button disabled={!canFork} onClick={handleFork}>
          ⑂ 从此节点 Fork
        </Button>
        <Button disabled>↻ 仅重跑此节点</Button>
        <Button disabled>📋 加入 fixture</Button>
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
