import { Card, Tag, Space, Spin, Empty, Alert, Modal, Button } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams, Link } from "react-router-dom";
import { useMemo, useState } from "react";

import { compareTasks } from "../api/client";

/**
 * 多任务对比页（M8）
 *
 * URL: /tasks/compare?ids=task1,task2,task3
 *
 * 按色号代码+色名对齐：每行一个色号，每列一个 task
 * 点 cell → 弹窗展示该 task 该色号的图 + prompt
 * 顶部"vs 第一列"按钮 → 弹窗展示两个 task 在该色号上的 prompt diff
 */
export default function TasksComparePage() {
  const [searchParams] = useSearchParams();
  const idsParam = searchParams.get("ids");
  const ids = useMemo(() => (idsParam ? idsParam.split(",").filter(Boolean) : []), [idsParam]);

  const [cellModal, setCellModal] = useState<{ row: any; cell: any } | null>(null);
  const [diffModal, setDiffModal] = useState<{ row: any; leftCell: any; rightCell: any } | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["tasks-compare", ids.sort().join(",")],
    queryFn: () => compareTasks(ids),
    enabled: ids.length >= 2,
  });

  if (ids.length < 2) {
    return (
      <div style={{ padding: 24 }}>
        <Empty description="请提供至少 2 个 task_id（URL 参数 ?ids=a,b,c）" />
      </div>
    );
  }
  if (isLoading) return <Spin tip="加载对比数据..." style={{ margin: 24 }} />;
  if (error || !data) return <Alert type="error" message="加载失败" style={{ margin: 24 }} />;

  const tasks = data.tasks;
  const rows = data.rows;
  const summary = data.summary;

  return (
    <div style={{ padding: 24, maxWidth: 1600, margin: "0 auto" }}>
      <Space style={{ marginBottom: 16, width: "100%", justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>横向对比 {tasks.length} 个任务</h2>
        <Link to="/tasks"><Button>← 历史列表</Button></Link>
      </Space>

      {!summary.all_same_style_no && (
        <Alert
          type="warning"
          message="款号不一致"
          description={`涉及款号：${summary.style_nos.join(", ")}。同色号对齐可能不准确。`}
          style={{ marginBottom: 16 }}
        />
      )}

      <Alert
        type="info"
        message={
          <Space wrap>
            <span>共 {summary.total_color_count} 种色号</span>
            <span>·</span>
            <span>{summary.common_color_count} 种全部 task 都有</span>
            <span>·</span>
            <span>按 <code>色号代码-营销色名</code> 对齐</span>
          </Space>
        }
        style={{ marginBottom: 16 }}
      />

      {/* 表头：每个 task 的元信息 */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "grid", gridTemplateColumns: `140px repeat(${tasks.length}, 1fr)`, gap: 12 }}>
          <div style={{ fontWeight: 600 }}>色号 ↓ / Task →</div>
          {tasks.map((t: any, i: number) => (
            <TaskHeaderCell key={t.id} task={t} index={i} />
          ))}
        </div>
      </Card>

      {/* 对齐 rows */}
      <Space direction="vertical" style={{ width: "100%" }} size="middle">
        {rows.map((row: any) => (
          <Card key={row.color_key} size="small">
            <div style={{ display: "grid", gridTemplateColumns: `140px repeat(${tasks.length}, 1fr)`, gap: 12, alignItems: "start" }}>
              {/* 色号列 */}
              <div>
                <div style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, color: "#999" }}>
                  {row.color_code}
                </div>
                <div style={{ fontWeight: 600 }}>{row.color_name}</div>
              </div>

              {/* 每个 task 一列 */}
              {row.cells.map((cell: any, ci: number) => (
                <CompareCell
                  key={ci}
                  cell={cell}
                  row={row}
                  isBaseline={ci === 0}
                  onShowDetail={() => setCellModal({ row, cell })}
                  onShowDiff={() => setDiffModal({
                    row,
                    leftCell: row.cells[0],
                    rightCell: cell,
                  })}
                />
              ))}
            </div>
          </Card>
        ))}
      </Space>

      {/* Cell 详情模态 */}
      <Modal
        open={!!cellModal}
        onCancel={() => setCellModal(null)}
        title={cellModal ? `${cellModal.row.color_key} · ${cellModal.cell.task_id?.slice(-8) || ""}` : ""}
        width="70vw"
        footer={null}
      >
        {cellModal && <CellDetail row={cellModal.row} cell={cellModal.cell} />}
      </Modal>

      {/* Diff 模态 */}
      <Modal
        open={!!diffModal}
        onCancel={() => setDiffModal(null)}
        title={diffModal ? `Prompt Diff · ${diffModal.row.color_key}` : ""}
        width="80vw"
        footer={null}
      >
        {diffModal && <PromptDiff leftCell={diffModal.leftCell} rightCell={diffModal.rightCell} row={diffModal.row} />}
      </Modal>
    </div>
  );
}


function TaskHeaderCell({ task, index }: { task: any; index: number }) {
  return (
    <div>
      <Space>
        <span style={{
          background: "#1677ff", color: "#fff",
          fontSize: 11, padding: "2px 6px", borderRadius: 8,
        }}>#{index + 1}</span>
        <Link to={`/tasks/${task.id}`} style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }}>
          {task.id.slice(-12)}
        </Link>
      </Space>
      <div style={{ fontSize: 12, color: "#666", marginTop: 4 }}>
        {task.style_no} · {task.progress_done}/{task.progress_total} ·
        {task.total_cost_usd != null ? ` $${task.total_cost_usd.toFixed(2)}` : ""}
        {task.total_elapsed_ms != null ? ` · ${(task.total_elapsed_ms / 1000).toFixed(0)}s` : ""}
      </div>
      <div style={{ fontSize: 11, color: "#999" }}>{task.created_at}</div>
    </div>
  );
}


function CompareCell({ cell, row, isBaseline, onShowDetail, onShowDiff }: any) {
  if (!cell || cell.image_status === "missing") {
    return (
      <div style={{
        background: "#fafafa", borderRadius: 4, padding: 16,
        textAlign: "center", color: "#999", fontSize: 12,
      }}>
        该 task 无此色号
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {/* 图片 */}
      <div
        style={{
          aspectRatio: "1/1.2",
          background: "#fafafa", borderRadius: 4, overflow: "hidden",
          display: "flex", alignItems: "center", justifyContent: "center",
          cursor: cell.image_url ? "pointer" : "default",
          border: "1px solid #eee",
        }}
        onClick={() => cell.image_url && onShowDetail()}
      >
        {cell.image_url ? (
          <img src={cell.image_url} alt={row.color_key} style={{ maxWidth: "100%", maxHeight: "100%" }} />
        ) : cell.image_status === "failed" ? (
          <div style={{ color: "#ff4d4f", textAlign: "center", padding: 8 }}>
            <div>✗ 失败</div>
            <div style={{ fontSize: 10, marginTop: 4 }}>{cell.error?.slice(0, 50)}</div>
          </div>
        ) : (
          <div style={{ color: "#999" }}>⏳ {cell.image_status}</div>
        )}
      </div>

      {/* 方案元摘要 */}
      {cell.plan_summary && (
        <div style={{ fontSize: 11 }}>
          <Space size={4} wrap>
            <Tag color="purple" style={{ margin: 0 }}>{cell.plan_summary.选用子主题编号}</Tag>
            <Tag style={{ margin: 0 }}>{cell.plan_summary.面组合}</Tag>
            <Tag style={{ margin: 0 }}>{cell.plan_summary.档组合}</Tag>
          </Space>
          <div style={{ color: "#666", marginTop: 4 }}>{cell.plan_summary.方案说明}</div>
        </div>
      )}

      {/* 操作 */}
      <Space size={4}>
        <Button size="small" onClick={onShowDetail}>👁 详情</Button>
        {!isBaseline && (
          <Button size="small" onClick={onShowDiff}>⇄ vs #1</Button>
        )}
      </Space>
    </div>
  );
}


function CellDetail({ row, cell }: { row: any; cell: any }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <div>
        {cell.image_url ? (
          <img src={cell.image_url} alt={row.color_key} style={{ width: "100%", borderRadius: 4 }} />
        ) : (
          <div style={{ background: "#fafafa", padding: 40, textAlign: "center" }}>图不可用</div>
        )}
      </div>
      <div>
        <Space direction="vertical" style={{ width: "100%" }} size={4}>
          <div><strong>方案编号：</strong>{cell.plan_id}</div>
          {cell.plan_summary && (
            <>
              <div>
                <Tag color="purple">{cell.plan_summary.选用子主题编号}</Tag>
                <span>{cell.plan_summary.选用子主题名称}</span>
              </div>
              <div><strong>面/档：</strong>{cell.plan_summary.面组合} / {cell.plan_summary.档组合}</div>
              <div><strong>适配度：</strong>{cell.plan_summary.适配度}</div>
              <div><strong>方案说明：</strong>{cell.plan_summary.方案说明}</div>
            </>
          )}
          {cell.elapsed_ms != null && (
            <div style={{ color: "#999", fontSize: 12 }}>
              生成耗时：{(cell.elapsed_ms / 1000).toFixed(1)}s
            </div>
          )}
          <details open>
            <summary style={{ cursor: "pointer", margin: "8px 0 4px", fontWeight: 600 }}>
              图生图 prompt
            </summary>
            <pre style={{
              background: "#1e1e1e", color: "#d4d4d4",
              padding: 12, fontSize: 11, whiteSpace: "pre-wrap",
              borderRadius: 4, maxHeight: 400, overflow: "auto",
            }}>
              {cell.image_prompt_used || "（无）"}
            </pre>
          </details>
        </Space>
      </div>
    </div>
  );
}


function PromptDiff({ leftCell, rightCell, row }: { leftCell: any; rightCell: any; row: any }) {
  const leftPrompt = leftCell?.image_prompt_used || "";
  const rightPrompt = rightCell?.image_prompt_used || "";
  const diffLines = computeUnifiedDiff(leftPrompt, rightPrompt);

  return (
    <div>
      <Alert
        type="info"
        message={`色号 ${row.color_key} · 左 = task #${leftCell.task_id?.slice(-8) || "?"} · 右 = task #${rightCell.task_id?.slice(-8) || "?"}`}
        style={{ marginBottom: 12 }}
      />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
        {leftCell.image_url && <img src={leftCell.image_url} alt="left" style={{ width: "100%", borderRadius: 4 }} />}
        {rightCell.image_url && <img src={rightCell.image_url} alt="right" style={{ width: "100%", borderRadius: 4 }} />}
      </div>
      <div style={{ fontWeight: 600, marginBottom: 8 }}>Prompt Diff</div>
      <pre style={{
        background: "#1e1e1e", padding: 12, borderRadius: 4,
        fontSize: 11, fontFamily: "ui-monospace, monospace",
        maxHeight: "50vh", overflow: "auto",
      }}>
        {diffLines.map((line, i) => (
          <div key={i} style={{ color: line.color }}>{line.text || " "}</div>
        ))}
      </pre>
    </div>
  );
}


// ----- 简单 unified diff（无外部依赖） ----- //
function computeUnifiedDiff(a: string, b: string): { text: string; color: string }[] {
  const aLines = a.split("\n");
  const bLines = b.split("\n");
  // 简单 LCS 行级 diff
  const n = aLines.length, m = bLines.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => Array(m + 1).fill(0));
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      if (aLines[i - 1] === bLines[j - 1]) dp[i][j] = dp[i - 1][j - 1] + 1;
      else dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  const out: { text: string; color: string }[] = [];
  let i = n, j = m;
  const stack: { text: string; color: string }[] = [];
  while (i > 0 && j > 0) {
    if (aLines[i - 1] === bLines[j - 1]) {
      stack.push({ text: "  " + aLines[i - 1], color: "#d4d4d4" });
      i--; j--;
    } else if (dp[i - 1][j] >= dp[i][j - 1]) {
      stack.push({ text: "- " + aLines[i - 1], color: "#ff7373" });
      i--;
    } else {
      stack.push({ text: "+ " + bLines[j - 1], color: "#a3d977" });
      j--;
    }
  }
  while (i > 0) { stack.push({ text: "- " + aLines[i - 1], color: "#ff7373" }); i--; }
  while (j > 0) { stack.push({ text: "+ " + bLines[j - 1], color: "#a3d977" }); j--; }
  while (stack.length) out.push(stack.pop()!);
  return out;
}
