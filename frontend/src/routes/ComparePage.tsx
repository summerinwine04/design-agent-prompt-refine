import { Layout, List, Card, Empty, Space, Checkbox, Spin, Tag, Button } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState, useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { listRuns, compareRuns } from "../api/client";

const { Sider, Content } = Layout;

/**
 * 横向对比页：左侧 run 多选列表，右侧指标对比表
 *
 * URL 参数：?ids=runA,runB,runC 可从外部直接打开预选
 *
 * 指标按 ABCDEF 6 类分组（见 eval/metrics.py）：
 *   A 结构完整性 / B 审计四项 / C 字段填充 / D 比例合规 / E 质量 / F 成本
 *
 * 高亮规则：以第一列为 baseline，其他列与之对比
 *   - 数值变好 → 绿色 ↑
 *   - 数值变差 → 红色 ↓
 *   - bool 由 false→true 绿，true→false 红
 */
export default function ComparePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const idsParam = searchParams.get("ids");
  const initialIds = useMemo(
    () => (idsParam ? idsParam.split(",").filter(Boolean) : []),
    [idsParam],
  );
  const [selected, setSelected] = useState<string[]>(initialIds);

  // 同步 URL（让对比链接可分享）
  useEffect(() => {
    if (selected.length > 0) {
      setSearchParams({ ids: selected.join(",") }, { replace: true });
    } else if (idsParam) {
      setSearchParams({}, { replace: true });
    }
  }, [selected, setSearchParams, idsParam]);

  const { data: allRuns } = useQuery({
    queryKey: ["runs-history"],
    queryFn: () => listRuns(),
  });

  const { data: comparison, isLoading: isComparing } = useQuery({
    queryKey: ["compare", [...selected].sort().join(",")],
    queryFn: () => compareRuns(selected),
    enabled: selected.length >= 2,
  });

  const toggle = (id: string) => {
    setSelected((s) =>
      s.includes(id) ? s.filter((x) => x !== id) : [...s, id],
    );
  };

  return (
    <Layout style={{ height: "calc(100vh - 64px)" }}>
      <Sider width={340} style={{ background: "#fff", padding: 16, overflowY: "auto" }}>
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Card size="small" title={`选择 Run (${selected.length} 已选)`} extra={
            selected.length > 0 ? (
              <Button size="small" onClick={() => setSelected([])}>清空</Button>
            ) : null
          }>
            {!allRuns || allRuns.length === 0 ? (
              <Empty description="暂无 run" />
            ) : (
              <List
                size="small"
                dataSource={allRuns}
                renderItem={(r: any) => {
                  const checked = selected.includes(r.id);
                  const order = selected.indexOf(r.id);
                  const isReal = !!r.total_tokens_in && r.total_tokens_in > 0;
                  const hasFinal = r.status === "succeeded" || r.status === "succeeded_with_audit_warnings";
                  return (
                    <List.Item
                      style={{ padding: "4px 8px", cursor: hasFinal ? "pointer" : "not-allowed", opacity: hasFinal ? 1 : 0.5 }}
                      onClick={() => hasFinal && toggle(r.id)}
                    >
                      <Space size={4} style={{ width: "100%" }}>
                        <Checkbox
                          checked={checked}
                          disabled={!hasFinal}
                          onChange={() => toggle(r.id)}
                          onClick={(e) => e.stopPropagation()}
                        />
                        {checked && (
                          <span style={{
                            background: "#1677ff", color: "#fff",
                            fontSize: 10, padding: "1px 6px", borderRadius: 8,
                          }}>{order + 1}</span>
                        )}
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontFamily: "ui-monospace, monospace", fontSize: 11, color: "#666" }}>
                            {r.id.slice(0, 17)}
                          </div>
                          <Space size={4}>
                            {isReal ? <Tag color="gold" style={{ margin: 0 }}>真跑</Tag> : <Tag style={{ margin: 0 }}>dry</Tag>}
                            <span style={{ fontSize: 11, color: "#999" }}>
                              {r.style_no} · {r.elapsed_ms ? `${(r.elapsed_ms / 1000).toFixed(0)}s` : "?"}
                            </span>
                          </Space>
                        </div>
                      </Space>
                    </List.Item>
                  );
                }}
              />
            )}
          </Card>
        </Space>
      </Sider>

      <Content style={{ padding: 24, overflowY: "auto" }}>
        {selected.length < 2 ? (
          <Empty description="左侧勾选 ≥ 2 个 run 开始对比" />
        ) : isComparing ? (
          <Spin tip="计算指标中..." />
        ) : (
          <CompareTable rows={comparison?.rows ?? []} errors={comparison?.errors ?? []} />
        )}
      </Content>
    </Layout>
  );
}


// ============================================================================
// 对比表格
// ============================================================================

interface CompareRow {
  run_id: string;
  label: string;
  trend_name: string;
  style_no: string;
  prompt_bundle: Record<string, string>;
  created_at: string;
  status: string;
  parent_run_id: string | null;
  fork_from_node: string | null;
  metrics: Record<string, any>;
}

const GROUP_NAMES: Record<string, string> = {
  A: "A. 结构完整性",
  B: "B. 审计四项（硬性规则）",
  C: "C. 字段填充质量",
  D: "D. 比例合规",
  E: "E. 质量（模型自评，仅参考）",
  F: "F. 成本",
};

// 指标方向：↑ 高就是好 / ↓ 低就是好 / undefined 无方向
const METRIC_DIRECTION: Record<string, "↑" | "↓"> = {
  A_plan_completion_rate: "↑",
  B_audit_overall_pass: "↑",
  B_audit1_face_dist_pass: "↑",
  B_audit2_cross_face_pass: "↑",
  B_audit2_cross_face_count: "↑",
  B_audit2_cross_face_variety: "↑",
  B_audit3_a_tier_pass: "↑",
  B_audit4_diversity_pass: "↑",
  B_audit4_diversity_unique_positions: "↑",
  B_audit4_diversity_cross_tier_count: "↑",
  C_anchors_filled_rate: "↑",
  C_face_tier_complete_rate: "↑",
  C_seven_section_complete_rate: "↑",
  C_visual_relationship_filled_rate: "↑",
  C_plan_desc_within_50_chars_rate: "↑",
  C_image_prompt_above_200_chars_rate: "↑",
  E_adaptation_score_avg: "↑",
  E_adaptation_score_min: "↑",
  E_trend_description_within_100: "↑",
  F_tokens_input: "↓",
  F_tokens_output: "↓",
  F_tokens_total: "↓",
  F_elapsed_seconds: "↓",
  F_llm_call_count_estimate: "↓",
  E_adaptation_score_stddev: "↓",
};

function CompareTable({ rows, errors }: { rows: CompareRow[]; errors: any[] }) {
  // 收集所有指标键
  const allMetricKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const r of rows) {
      for (const k of Object.keys(r.metrics || {})) {
        if (k.startsWith("schema_version") || k.startsWith("_")) continue;
        keys.add(k);
      }
    }
    return Array.from(keys).sort();
  }, [rows]);

  // 按前缀字母分组
  const groups = useMemo(() => {
    const out: Record<string, string[]> = {};
    for (const k of allMetricKeys) {
      const prefix = k[0];
      if (!out[prefix]) out[prefix] = [];
      out[prefix].push(k);
    }
    return out;
  }, [allMetricKeys]);

  return (
    <div>
      {errors.length > 0 && (
        <Card size="small" style={{ marginBottom: 16, borderColor: "#faad14" }}>
          <strong>有 {errors.length} 个 run 加载失败：</strong>
          {errors.map((e) => (
            <div key={e.run_id} style={{ fontSize: 12 }}>
              {e.run_id}: {e.error}
            </div>
          ))}
        </Card>
      )}

      {/* 表头：run label + prompt bundle */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "grid", gridTemplateColumns: `200px repeat(${rows.length}, 1fr)`, gap: 8 }}>
          <div style={{ fontWeight: 600 }}>Run 信息</div>
          {rows.map((r, i) => (
            <div key={r.run_id}>
              <div style={{ fontWeight: 600 }}>
                <span style={{
                  background: "#1677ff", color: "#fff",
                  fontSize: 10, padding: "1px 6px", borderRadius: 8, marginRight: 6,
                }}>{i + 1}</span>
                {r.label}
              </div>
              <div style={{ fontSize: 11, color: "#888", fontFamily: "ui-monospace, monospace" }}>
                {r.run_id}
              </div>
              {r.parent_run_id && (
                <div style={{ fontSize: 11, color: "#888" }}>
                  ⑂ Fork from {r.parent_run_id.slice(-8)}
                </div>
              )}
              <Space size={4} style={{ marginTop: 4 }} wrap>
                {Object.entries(r.prompt_bundle).map(([k, v]) => (
                  <Tag key={k} color="purple" style={{ margin: 0, fontSize: 10 }}>
                    {k}: {(v as string).split("@").pop()}
                  </Tag>
                ))}
              </Space>
            </div>
          ))}
        </div>
      </Card>

      {/* 各类指标表 */}
      {Object.entries(groups).map(([prefix, keys]) => (
        <Card key={prefix} size="small" title={GROUP_NAMES[prefix] || prefix} style={{ marginBottom: 12 }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ background: "#fafafa" }}>
                <th style={{ textAlign: "left", padding: "4px 8px", width: 240 }}>指标</th>
                <th style={{ width: 30 }}></th>
                {rows.map((r, i) => (
                  <th key={r.run_id} style={{ textAlign: "right", padding: "4px 8px" }}>
                    #{i + 1}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => {
                const baseline = rows[0]?.metrics[k];
                const direction = METRIC_DIRECTION[k];
                return (
                  <tr key={k} style={{ borderTop: "1px solid #f0f0f0" }}>
                    <td style={{ padding: "4px 8px", fontFamily: "ui-monospace, monospace" }}>{k}</td>
                    <td style={{ color: "#999", textAlign: "center" }}>{direction || ""}</td>
                    {rows.map((r, i) => {
                      const v = r.metrics[k];
                      return (
                        <td key={r.run_id} style={{
                          padding: "4px 8px",
                          textAlign: "right",
                          fontFamily: "ui-monospace, monospace",
                          ...diffStyle(baseline, v, direction, i),
                        }}>
                          {formatValue(v)}
                          {i > 0 && diffSuffix(baseline, v, direction)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      ))}
    </div>
  );
}


function formatValue(v: any): string {
  if (v === undefined || v === null) return "—";
  if (typeof v === "boolean") return v ? "✓" : "✗";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return v.toLocaleString();
    return v.toFixed(3);
  }
  return String(v);
}

function diffStyle(baseline: any, v: any, dir: "↑" | "↓" | undefined, col: number): React.CSSProperties {
  if (col === 0) return {};

  // bool 差异
  if (typeof baseline === "boolean" && typeof v === "boolean") {
    if (baseline === v) return {};
    if (dir) {
      const better = (dir === "↑" && v) || (dir === "↓" && !v);
      return {
        background: better ? "#f6ffed" : "#fff1f0",
        color: better ? "#389e0d" : "#cf1322",
        fontWeight: 600,
      };
    }
    // 无方向但有差异 → 蓝色
    return { background: "#e6f4ff", color: "#1677ff", fontWeight: 600 };
  }

  // 数值差异
  if (typeof baseline === "number" && typeof v === "number") {
    if (baseline === v) return {};
    if (dir) {
      const isUp = v > baseline;
      const better = (dir === "↑" && isUp) || (dir === "↓" && !isUp);
      return {
        background: better ? "#f6ffed" : "#fff1f0",
        color: better ? "#389e0d" : "#cf1322",
        fontWeight: 600,
      };
    }
    // 无方向但数值有差异 → 蓝色
    return { background: "#e6f4ff", color: "#1677ff", fontWeight: 600 };
  }

  return {};
}

function diffSuffix(baseline: any, v: any, dir: "↑" | "↓" | undefined): string {
  if (typeof baseline !== "number" || typeof v !== "number" || baseline === v) return "";
  const delta = v - baseline;
  const arrow = delta > 0 ? "↑" : "↓";
  const sign = delta > 0 ? "+" : "";
  if (Math.abs(delta) < 1 && Math.abs(delta) > 0.001) {
    return ` ${arrow}${sign}${delta.toFixed(3)}`;
  }
  return ` ${arrow}${sign}${Math.round(delta).toLocaleString()}`;
}
