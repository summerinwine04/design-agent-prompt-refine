import { Card, Table, Tag, Space, Tooltip, DatePicker, Drawer, Spin, Empty, Statistic } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { getBillingSummary, getBillingDelivery } from "../api/client";

/**
 * 💰 账单：交付任务级 token / 成本统计
 *
 * 口径（见 docs/账单Tab PRD，已拍板）：
 *   交付任务 = 主 run + fork 链 + 生图任务；缓存命中计 0 并展示节省估算；
 *   失败已烧 token 照常计入；生图失败展示、计 $0；单价在设置页维护。
 */

const MODE_TAG: Record<string, { label: string; color: string }> = {
  MULTI_TOPIC: { label: "🎨 多主题", color: "default" },
  SINGLE_TOPIC_STRONG: { label: "🧬 强单主题", color: "purple" },
  COLLECTION_2SKU: { label: "👕👖 成套", color: "geekblue" },
};

const fmtTok = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0));
const fmtDur = (ms: number) => {
  if (!ms) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60 ? `${s % 60}s` : ""}`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
};

export default function BillingPage() {
  const [range, setRange] = useState<[string, string] | null>(null);
  const [detailRunId, setDetailRunId] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["billing-summary", range],
    queryFn: () => getBillingSummary(range ? { date_from: range[0], date_to: range[1] } : undefined),
    refetchInterval: 30000,
  });

  const totals = data?.totals;

  return (
    <div style={{ padding: 16, maxWidth: 1320, margin: "0 auto" }}>
      <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 12 }} wrap>
        <Space>
          <span style={{ fontWeight: 600, fontSize: 16 }}>💰 账单</span>
          <DatePicker.RangePicker
            size="small"
            onChange={(_d, ds) => setRange(ds && ds[0] ? [ds[0], ds[1]] : null)}
          />
        </Space>
      </Space>

      {/* 合计卡 */}
      {totals && (
        <Card size="small" style={{ marginBottom: 12 }}>
          <Space size={32} wrap>
            <Statistic title="交付任务" value={totals.deliveries} />
            <Statistic title="设计 token in" value={fmtTok(totals.tokens_in)} />
            <Statistic title="设计 token out" value={fmtTok(totals.tokens_out)} />
            <Statistic title="生图（成功/失败）" value={`${totals.images_ok} / ${totals.images_failed}`} />
            <Tooltip title="生图 API 的 usage token（新任务起记录，历史任务未落盘显示 0）">
              <Statistic
                title="生图 token (in/out)"
                value={`${fmtTok(totals.gen_tokens_in)} / ${fmtTok(totals.gen_tokens_out)}`}
              />
            </Tooltip>
            <Tooltip title="单款 token = 设计(in+out) ÷ 生图成功张数——每出一张成品图平均摊多少设计消耗">
              <Statistic
                title="平均单款 token"
                value={totals.avg_tokens_per_image != null ? fmtTok(totals.avg_tokens_per_image) : "—"}
                valueStyle={{ color: "#1677ff" }}
              />
            </Tooltip>
            <Tooltip title={`缓存命中 ${totals.cache_hits} 次`}>
              <Statistic
                title="♻️ 缓存节省 token（估）"
                value={fmtTok(totals.cache_saved_tokens_est)}
                valueStyle={{ color: "#389e0d" }}
              />
            </Tooltip>
            <Statistic title="设计耗时" value={fmtDur(totals.design_elapsed_ms)} />
            <Statistic title="生图耗时" value={fmtDur(totals.gen_elapsed_ms)} />
          </Space>
        </Card>
      )}

      <Card size="small" title="交付任务汇总（主 run + fork 链 + 生图归并为一行）">
        <Table
          size="small"
          rowKey="main_run_id"
          loading={isLoading}
          dataSource={data?.rows || []}
          pagination={{ pageSize: 20 }}
          onRow={(r: any) => ({ onClick: () => setDetailRunId(r.main_run_id), style: { cursor: "pointer" } })}
          columns={[
            {
              title: "时间", dataIndex: "created_at", width: 150,
              render: (v: string) => <span style={{ fontSize: 12, color: "#666" }}>{(v || "").slice(0, 16)}</span>,
            },
            {
              title: "交付任务",
              render: (_: any, r: any) => (
                <Space size={4} wrap>
                  <Tag color={MODE_TAG[r.design_mode]?.color}>{MODE_TAG[r.design_mode]?.label || r.design_mode}</Tag>
                  <span style={{ fontSize: 12 }}>{r.trend_name?.slice(0, 10)} × {r.style_no}</span>
                  {r.fork_count > 0 && <Tag style={{ fontSize: 10 }}>+{r.fork_count} fork</Tag>}
                </Space>
              ),
            },
            {
              title: "设计 token (in/out)", width: 170,
              sorter: (a: any, b: any) => (a.tokens_in + a.tokens_out) - (b.tokens_in + b.tokens_out),
              render: (_: any, r: any) => (
                <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }}>
                  <strong>{fmtTok(r.tokens_in)}</strong> / {fmtTok(r.tokens_out)}
                </span>
              ),
            },
            {
              title: "生图", width: 100,
              render: (_: any, r: any) => (
                <span style={{ fontSize: 12 }}>
                  {r.images_ok} 张{r.images_failed > 0 && <span style={{ color: "#cf1322" }}> +{r.images_failed} 败</span>}
                </span>
              ),
            },
            {
              title: "生图 token (in/out)", width: 150,
              render: (_: any, r: any) => (
                (r.gen_tokens_in || r.gen_tokens_out) ? (
                  <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }}>
                    {fmtTok(r.gen_tokens_in)} / {fmtTok(r.gen_tokens_out)}
                  </span>
                ) : <span style={{ color: "#ddd" }}>—</span>
              ),
            },
            {
              title: (
                <Tooltip title="单款 token = 设计(in+out) ÷ 生图成功张数">
                  <span>单款 token</span>
                </Tooltip>
              ),
              width: 110,
              sorter: (a: any, b: any) => (a.avg_tokens_per_image || 0) - (b.avg_tokens_per_image || 0),
              render: (_: any, r: any) => r.avg_tokens_per_image != null ? (
                <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, color: "#1677ff" }}>
                  {fmtTok(r.avg_tokens_per_image)}
                </span>
              ) : <span style={{ color: "#ddd" }}>—</span>,
            },
            {
              title: "耗时(设计/生图)", width: 130,
              sorter: (a: any, b: any) =>
                (a.design_elapsed_ms + a.gen_elapsed_ms) - (b.design_elapsed_ms + b.gen_elapsed_ms),
              render: (_: any, r: any) => (
                <span style={{ fontSize: 12, color: "#666" }}>
                  {fmtDur(r.design_elapsed_ms)} / {fmtDur(r.gen_elapsed_ms)}
                </span>
              ),
            },
            {
              title: "♻️ 节省", width: 90,
              render: (_: any, r: any) => r.cache_hits > 0 ? (
                <Tooltip title={`缓存命中 ${r.cache_hits} 次 ≈ 省 ${fmtTok(r.cache_saved_tokens_est)} token`}>
                  <Tag color="green" style={{ fontSize: 10 }}>×{r.cache_hits}</Tag>
                </Tooltip>
              ) : <span style={{ color: "#ddd" }}>—</span>,
            },
            {
              title: "状态", dataIndex: "status", width: 90,
              render: (v: string) => (
                <Tag color={v === "succeeded" ? "success" : v === "failed" ? "error" : v === "running" ? "processing" : "warning"} style={{ fontSize: 10 }}>
                  {v === "succeeded_with_audit_warnings" ? "⚠审计" : v}
                </Tag>
              ),
            },
          ]}
        />
      </Card>

      <Drawer
        title="交付任务明细"
        width={880}
        open={!!detailRunId}
        onClose={() => setDetailRunId(null)}
        destroyOnClose
      >
        {detailRunId && <DeliveryDetail mainRunId={detailRunId} />}
      </Drawer>
    </div>
  );
}


function DeliveryDetail({ mainRunId }: { mainRunId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["billing-delivery", mainRunId],
    queryFn: () => getBillingDelivery(mainRunId),
  });

  if (isLoading) return <Spin />;
  if (!data) return <Empty />;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space size={16} wrap>
        <Tag color={MODE_TAG[data.design_mode]?.color}>{MODE_TAG[data.design_mode]?.label || data.design_mode}</Tag>
        <span>{data.trend_name} × {data.style_no}</span>
        <Link to={`/?run=${mainRunId}`} style={{ fontSize: 12 }}>工作台查看 →</Link>
      </Space>

      <Space size={24} wrap>
        <Statistic title="设计 token in" value={fmtTok(data.totals.tokens_in)} />
        <Statistic title="设计 token out" value={fmtTok(data.totals.tokens_out)} />
        <Tooltip title="生图 API 的 usage token（新任务起记录；历史任务未落盘显示 0）">
          <Statistic
            title="生图 token (in/out)"
            value={`${fmtTok(data.totals.gen_tokens_in || 0)} / ${fmtTok(data.totals.gen_tokens_out || 0)}`}
          />
        </Tooltip>
        <Statistic title="设计耗时" value={fmtDur(data.totals.design_elapsed_ms || 0)} />
        <Statistic title="生图耗时" value={fmtDur(data.totals.gen_elapsed_ms || 0)} />
        {(() => {
          const imagesOk = (data.gen_tasks || []).reduce((s: number, t: any) => s + (t.images_ok || 0), 0);
          return (
            <Tooltip title="单款 token = 设计(in+out) ÷ 生图成功张数">
              <Statistic
                title="平均单款 token"
                value={imagesOk > 0 ? fmtTok(Math.round((data.totals.tokens_in + data.totals.tokens_out) / imagesOk)) : "—"}
                valueStyle={{ color: "#1677ff" }}
              />
            </Tooltip>
          );
        })()}
      </Space>

      <Card size="small" title={`设计管线节点（${data.nodes.length} 次调用，含 ${data.runs.length} 个 run）`}>
        <Table
          size="small"
          rowKey={(r: any, i?: number) => `${r.run_id}-${r.name}-${i}`}
          dataSource={data.nodes}
          pagination={data.nodes.length > 30 ? { pageSize: 30 } : false}
          columns={[
            { title: "节点", dataIndex: "biz_id", width: 70, render: (v: string) => <Tag style={{ fontSize: 10 }}>{v}</Tag> },
            {
              title: "名称", dataIndex: "name",
              render: (v: string, r: any) => (
                <span style={{ fontSize: 12, color: r.cached ? "#999" : undefined }}>
                  {v}{r.failed && <Tag color="error" style={{ fontSize: 9, marginLeft: 4 }}>failed</Tag>}
                  {r.repair && <Tag color="orange" style={{ fontSize: 9, marginLeft: 4 }}>修复</Tag>}
                </span>
              ),
            },
            { title: "来源", dataIndex: "source", width: 100, render: (v: string) => <span style={{ fontSize: 11, color: "#888" }}>{v}</span> },
            {
              title: "token in/out", width: 120,
              render: (_: any, r: any) => (
                <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 11, color: r.cached ? "#bbb" : undefined }}>
                  {fmtTok(r.tokens_in)} / {fmtTok(r.tokens_out)}
                </span>
              ),
            },
            { title: "耗时", dataIndex: "elapsed_ms", width: 70, render: (v: number) => <span style={{ fontSize: 11, color: "#888" }}>{v ? `${(v / 1000).toFixed(1)}s` : "—"}</span> },
            {
              title: "缓存", dataIndex: "cached", width: 110,
              render: (v: string | null, r: any) => v ? (
                <Tooltip title={`命中缓存，估算省 ${fmtTok(r.cache_saved_tokens_est)} token`}>
                  <Tag color="green" style={{ fontSize: 10 }}>{v === "style" ? "♻️ 款级" : "⚡ fork"}</Tag>
                </Tooltip>
              ) : <span style={{ color: "#eee" }}>—</span>,
            },
          ]}
        />
      </Card>

      <Card size="small" title={`生图任务（${data.gen_tasks.length} 个）`}>
        {data.gen_tasks.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未生图" />
        ) : (
          <Table
            size="small"
            rowKey="task_id"
            dataSource={data.gen_tasks}
            pagination={false}
            expandable={{
              expandedRowRender: (t: any) => (
                <div style={{ fontSize: 11 }}>
                  {(t.plans || []).map((p: any) => (
                    <div key={p.plan_id} style={{ padding: "2px 0" }}>
                      <Tag color={p.status === "succeeded" ? "success" : "error"} style={{ fontSize: 9 }}>{p.status}</Tag>
                      <span style={{ fontFamily: "ui-monospace, monospace" }}>{p.plan_id}</span>
                      <span style={{ color: "#999", marginLeft: 8 }}>{p.elapsed_ms ? `${(p.elapsed_ms / 1000).toFixed(0)}s` : ""}</span>
                      {(p.tokens_in || p.tokens_out) ? (
                        <span style={{ color: "#888", marginLeft: 8, fontFamily: "ui-monospace, monospace" }}>
                          {fmtTok(p.tokens_in)} / {fmtTok(p.tokens_out)} tok
                        </span>
                      ) : null}
                    </div>
                  ))}
                </div>
              ),
            }}
            columns={[
              { title: "任务", dataIndex: "task_id", render: (v: string) => <Link to={`/tasks/${v}`} style={{ fontFamily: "ui-monospace, monospace", fontSize: 11 }}>{v}</Link> },
              { title: "成功", dataIndex: "images_ok", width: 60 },
              { title: "失败", dataIndex: "images_failed", width: 70, render: (v: number) => v > 0 ? <span style={{ color: "#cf1322" }}>{v}</span> : "—" },
              {
                title: "token in/out", width: 120,
                render: (_: any, r: any) => (r.tokens_in || r.tokens_out) ? (
                  <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 11 }}>
                    {fmtTok(r.tokens_in)} / {fmtTok(r.tokens_out)}
                  </span>
                ) : <Tooltip title="历史任务未记录生图 usage，新任务起自动记录"><span style={{ color: "#ddd" }}>—</span></Tooltip>,
              },
              { title: "耗时", dataIndex: "elapsed_ms", width: 70, render: (v: number) => v ? `${(v / 1000).toFixed(0)}s` : "—" },
              { title: "时间", dataIndex: "created_at", render: (v: string) => <span style={{ fontSize: 11, color: "#888" }}>{v}</span> },
            ]}
          />
        )}
      </Card>
    </Space>
  );
}
