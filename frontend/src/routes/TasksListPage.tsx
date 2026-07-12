import { Card, Empty, Tag, Space, Spin, Button, Progress, Popconfirm, message, Checkbox, Tooltip } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";

import { listTasks, deleteTask, type TaskSummary } from "../api/client";


// v4 节点 → 简洁中文标签
const NODE_LABELS: Record<string, string> = {
  "2.1": "款式分析",
  "2.2": "颜色识别",
  "2.3": "性别规划",
  "2.4": "主题选择",
  "2.5": "单色设计",
  "2.8": "局部重设计",
};

// 从 "01_style_analysis.md@v3" 抽出 "v3"；@current → "current"
function extractVersion(label: string | undefined): string {
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

const STATUS_LABEL: Record<string, string> = {
  pending: "待启动",
  running: "运行中",
  completed: "全部成功",
  partial: "部分失败",
  failed: "失败",
};

/**
 * 生图任务历史列表
 * 每条 task 默认折叠成一行摘要；点击 → 详情页 /tasks/:id
 * 勾选 ≥2 个 → 顶部出现「对比所选」按钮 → 跳 /tasks/compare?ids=...
 */
export default function TasksListPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const { data, isLoading } = useQuery({
    queryKey: ["tasks"],
    queryFn: listTasks,
    refetchInterval: 5000,
  });

  const delMut = useMutation({
    mutationFn: (id: string) => deleteTask(id),
    onSuccess: () => {
      message.success("已删除");
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err: any) => message.error("删除失败：" + (err?.message || String(err))),
  });

  const toggle = (id: string) => {
    setSelected((s) => {
      const ns = new Set(s);
      if (ns.has(id)) ns.delete(id);
      else ns.add(id);
      return ns;
    });
  };

  if (isLoading) return <Spin style={{ margin: 24 }} />;

  return (
    <div style={{ padding: 24, maxWidth: 1100, margin: "0 auto" }}>
      <Space style={{ marginBottom: 16, width: "100%", justifyContent: "space-between" }}>
        <Space>
          <h2 style={{ margin: 0 }}>生图任务历史</h2>
          {selected.size > 0 && <Tag color="blue">已选 {selected.size}</Tag>}
        </Space>
        <Space>
          {selected.size >= 2 ? (
            <Link to={`/tasks/compare?ids=${Array.from(selected).join(",")}`}>
              <Button type="primary">⇄ 对比所选 {selected.size} 个 →</Button>
            </Link>
          ) : selected.size === 1 ? (
            <span style={{ color: "#999", fontSize: 12 }}>再选 1 个开启对比</span>
          ) : null}
          {selected.size > 0 && (
            <Button size="small" onClick={() => setSelected(new Set())}>清空选择</Button>
          )}
          <Link to="/tasks/gallery">
            <Button type="primary" ghost>🖼️ 查看全部生图</Button>
          </Link>
          <Link to="/"><Button>← 工作台</Button></Link>
        </Space>
      </Space>

      {!data || data.length === 0 ? (
        <Empty
          description={
            <div>
              <div>还没生过图。</div>
              <div style={{ marginTop: 8, color: "#888", fontSize: 12 }}>
                先在工作台跑通 step2，再点 RunHeader 的「→ 应用方案生图」按钮进入新建流程。
              </div>
            </div>
          }
        />
      ) : (
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          {data.map((t) => (
            <TaskRow
              key={t.id}
              task={t}
              checked={selected.has(t.id)}
              onToggle={() => toggle(t.id)}
              onDelete={() => delMut.mutate(t.id)}
            />
          ))}
        </Space>
      )}
    </div>
  );
}


function TaskRow({
  task, checked, onToggle, onDelete,
}: {
  task: TaskSummary;
  checked: boolean;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const percent = task.progress_total > 0 ? Math.round((task.progress_done / task.progress_total) * 100) : 0;
  // 只有跑完的 task 才能加入对比
  const canCompare = task.status === "completed" || task.status === "partial";

  return (
    <Card size="small" hoverable style={{ borderColor: checked ? "#1677ff" : undefined, background: checked ? "#e6f4ff" : undefined }}>
      <Space direction="vertical" style={{ width: "100%" }} size={4}>
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <Space>
            <Checkbox
              checked={checked}
              disabled={!canCompare}
              onChange={onToggle}
              onClick={(e) => e.stopPropagation()}
            />
            <Link to={`/tasks/${task.id}`} style={{ fontWeight: 600, fontFamily: "ui-monospace, monospace" }}>
              {task.id}
            </Link>
            <Tag color={STATUS_COLOR[task.status] || "default"}>{STATUS_LABEL[task.status] || task.status}</Tag>
          </Space>
          <Space>
            <Link to={`/tasks/${task.id}`}>
              <Button size="small" type="primary">查看详情 →</Button>
            </Link>
            <Popconfirm title="删除归档？图片文件也会一并删除" onConfirm={onDelete} okText="删" cancelText="取消">
              <Button size="small" danger>删除</Button>
            </Popconfirm>
          </Space>
        </Space>

        <Space size="middle" style={{ fontSize: 13, color: "#666" }} wrap>
          <span>{task.trend_name} × {task.style_no}</span>
          <span>·</span>
          <span>{task.progress_done} / {task.progress_total} 张</span>
          {task.total_cost_usd != null && (
            <>
              <span>·</span>
              <span>≈ ${task.total_cost_usd.toFixed(2)}</span>
            </>
          )}
          {task.total_elapsed_ms != null && (
            <>
              <span>·</span>
              <span>{(task.total_elapsed_ms / 1000).toFixed(0)}s</span>
            </>
          )}
          <span>·</span>
          <span>{task.created_at}</span>
        </Space>

        <Progress
          percent={percent}
          size="small"
          status={
            task.status === "failed" ? "exception" :
            task.status === "running" ? "active" :
            task.status === "completed" ? "success" :
            "normal"
          }
          showInfo={false}
        />

        {/* 源 step2 run 的 prompt 版本（2.1 / 2.2 / 2.3 / 2.4）*/}
        <Space size={4} wrap style={{ fontSize: 11 }}>
          <span style={{ color: "#999" }}>Step2 Prompt 版本:</span>
          {/* 任务列表行宽有限，只展示 v4 最关心的 4 个 prompt 调试点 */}
          {["2.1", "2.4", "2.5", "2.8"].map((nodeId) => {
            const label = task.source_prompt_bundle?.[nodeId];
            const version = extractVersion(label);
            const isNonDefault = label && version !== "current";
            return (
              <Tooltip key={nodeId} title={label || `节点 ${nodeId} 无版本信息`}>
                <Tag
                  color={isNonDefault ? "purple" : "default"}
                  style={{ margin: 0, fontSize: 10, padding: "0 6px", lineHeight: "18px" }}
                >
                  {nodeId} {NODE_LABELS[nodeId]} · <strong>{version}</strong>
                </Tag>
              </Tooltip>
            );
          })}
        </Space>

        <span style={{ fontSize: 11, color: "#999", fontFamily: "ui-monospace, monospace" }}>
          源 step2 run: {task.source_step2_run_id}
        </span>
      </Space>
    </Card>
  );
}
