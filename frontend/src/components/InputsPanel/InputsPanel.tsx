import { Button, Select, Form, Input, InputNumber, Space, Card, Alert, Tag, Tooltip, List, Checkbox } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { listTrends, listStyles, listFixtures, listRuns } from "../../api/client";
import { useRunStore } from "../../store/runStore";


const STATUS_COLOR: Record<string, string> = {
  running: "processing",
  succeeded: "success",
  succeeded_with_audit_warnings: "warning",
  failed: "error",
};

/**
 * 左侧输入栏：选趋势 / 款号 / 性别比 / K，触发 POST /runs。
 *
 * Backend 接口要的字段（见 backend/schemas.py RunCreateRequest）：
 *   - trend_json_path  (str)
 *   - ref_image_path   (str)
 *   - color_folder     (str)
 *   - gender_ratio     (str)
 *   - num_designs_k    (int)
 *   - dry_run          (bool)
 *   - prompt_bundle    (default {})
 *
 * 关键：款图正面图 = ai-supply/款图/{style_no}.jpg，色号文件夹 = ai-supply/款图/{style_no}/
 * 选款号时自动联动 color_folder。
 */
export default function InputsPanel() {
  const { data: trends } = useQuery({ queryKey: ["trends"], queryFn: listTrends });
  const { data: styles } = useQuery({ queryKey: ["styles"], queryFn: listStyles });
  const { data: fixtures } = useQuery({ queryKey: ["fixtures"], queryFn: listFixtures });
  const startRun = useRunStore((s) => s.startRun);
  const loadRun = useRunStore((s) => s.loadRun);
  const currentRunId = useRunStore((s) => s.currentRunId);
  const isRunning = useRunStore((s) => s.isRunning);
  const error = useRunStore((s) => s.error);
  const promptOverrides = useRunStore((s) => s.promptOverrides);
  const clearPromptOverrides = useRunStore((s) => s.clearPromptOverrides);
  const overrideEntries = Object.entries(promptOverrides);

  // 历史 run 列表（最近 20 条）
  const { data: historyRuns } = useQuery({
    queryKey: ["runs-history"],
    queryFn: () => listRuns(),
    refetchInterval: 5000,    // 5 秒拉一次，新 run 自动出现
  });

  // M5.3: 多选状态（用于"对比"快捷入口）
  const [compareSelected, setCompareSelected] = useState<Set<string>>(new Set());
  const toggleCompare = (id: string) => {
    setCompareSelected((s) => {
      const ns = new Set(s);
      if (ns.has(id)) ns.delete(id);
      else ns.add(id);
      return ns;
    });
  };

  const [form] = Form.useForm();

  // 款号选项里同时藏 ref_image_path + color_folder，选中时一并填
  const styleOptions = useMemo(
    () => (styles?.styles ?? []).map((s: any) => ({
      value: s.style_no,
      label: `${s.style_no}（${s.color_count} 色）`,
      ref_image: s.ref_image,
      color_folder: s.color_folder,
    })),
    [styles],
  );

  const trendOptions = useMemo(
    () => (trends?.trends ?? []).map((t: any) => ({
      value: t.json_path,
      label: `${t.name}${t.parsed ? "" : "（未解析）"}`,
      disabled: !t.parsed,
    })),
    [trends],
  );

  const submit = (dryRun: boolean) => {
    form.validateFields().then((values) => {
      // 把 style_no 转回 ref_image + color_folder
      const styleOpt = styleOptions.find((o: any) => o.value === values.style_no);
      if (!styleOpt) return;
      const payload = {
        trend_json_path: values.trend_json_path,
        ref_image_path: styleOpt.ref_image,
        color_folder: styleOpt.color_folder,
        gender_ratio: values.gender_ratio,
        num_designs_k: values.num_designs_k,
        dry_run: dryRun,
        prompt_bundle: { versions: {} },
      };
      startRun(payload);
    }).catch(() => {
      // validateFields rejected (有空字段)，AntD 自动高亮，无需额外提示
    });
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {error && (
        <Alert
          type="error"
          message="启动失败"
          description={error}
          closable
          onClose={() => useRunStore.setState({ error: null })}
        />
      )}

      <Card size="small" title="输入">
        <Form form={form} layout="vertical" size="small" disabled={isRunning}>
          <Form.Item
            label="趋势报告"
            name="trend_json_path"
            rules={[{ required: true, message: "请选择趋势报告" }]}
          >
            <Select placeholder="选趋势..." options={trendOptions} />
          </Form.Item>

          <Form.Item
            label="款号"
            name="style_no"
            rules={[{ required: true, message: "请选择款号" }]}
            help="款图正面图 + 色号文件夹会自动联动"
          >
            <Select placeholder="选款号..." options={styleOptions} />
          </Form.Item>

          <Form.Item
            label="性别比"
            name="gender_ratio"
            initialValue="男女比接近1:1"
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>

          <Form.Item label="K（每色方案数）" name="num_designs_k" initialValue={1}>
            <InputNumber min={1} max={5} style={{ width: "100%" }} />
          </Form.Item>

          <Space>
            <Button type="primary" onClick={() => submit(false)} loading={isRunning} disabled={isRunning}>
              ▶ 真跑（烧 token）
            </Button>
            <Button onClick={() => submit(true)} disabled={isRunning}>
              空跑（dry-run）
            </Button>
          </Space>
        </Form>
      </Card>

      {overrideEntries.length > 0 && (
        <Card
          size="small"
          title={
            <Space>
              Prompt 版本覆盖
              <Tooltip title="下次提交会强制用这些版本。来自 PromptDrawer 的「用此版本跑」">
                <span style={{ color: "#999", fontSize: 12 }}>?</span>
              </Tooltip>
            </Space>
          }
          extra={
            <Button size="small" onClick={clearPromptOverrides} disabled={isRunning}>
              清除
            </Button>
          }
        >
          <Space wrap>
            {overrideEntries.map(([nodeBizId, version]) => (
              <Tag key={nodeBizId} color="purple">
                {nodeBizId} → {version}
              </Tag>
            ))}
          </Space>
        </Card>
      )}

      <Card
        size="small"
        title={
          <Space>
            历史 Run
            <Tooltip title="点条目本身加载进工作台；勾 checkbox 选 ≥2 个后点对比">
              <span style={{ color: "#999", fontSize: 12 }}>?</span>
            </Tooltip>
          </Space>
        }
        extra={
          compareSelected.size >= 2 ? (
            <Link to={`/compare?ids=${Array.from(compareSelected).join(",")}`}>
              <Button size="small" type="primary">
                对比 {compareSelected.size} 个 →
              </Button>
            </Link>
          ) : compareSelected.size === 1 ? (
            <span style={{ fontSize: 11, color: "#999" }}>再选 1 个</span>
          ) : null
        }
      >
        {!historyRuns || historyRuns.length === 0 ? (
          <div style={{ color: "#999", fontSize: 12 }}>暂无历史</div>
        ) : (
          <List
            size="small"
            dataSource={historyRuns.slice(0, 20)}
            renderItem={(r: any) => {
              const isActive = currentRunId === r.id;
              const isReal = !!r.total_tokens_in && r.total_tokens_in > 0;
              const hasFinal = r.status === "succeeded" || r.status === "succeeded_with_audit_warnings";
              const checked = compareSelected.has(r.id);
              return (
                <List.Item
                  style={{
                    cursor: "pointer",
                    background: isActive ? "#e6f4ff" : undefined,
                    padding: "4px 8px",
                  }}
                >
                  <Space direction="vertical" size={0} style={{ width: "100%" }}>
                    <Space size={4} style={{ width: "100%" }}>
                      <Tooltip title={hasFinal ? "勾选加入对比" : "未完成的 run 不能对比"}>
                        <Checkbox
                          checked={checked}
                          disabled={!hasFinal}
                          onChange={() => toggleCompare(r.id)}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Tooltip>
                      <div style={{ flex: 1, minWidth: 0 }} onClick={() => loadRun(r.id)}>
                        <Space size={6} style={{ fontSize: 11 }} wrap>
                          <span style={{ fontFamily: "ui-monospace, monospace", color: "#666" }}>
                            {r.id.slice(0, 17)}
                          </span>
                          <Tag color={STATUS_COLOR[r.status] || "default"} style={{ margin: 0 }}>
                            {r.audit_passed === true ? "✓审计" : r.audit_passed === false ? "⚠审计" : r.status}
                          </Tag>
                          {isReal && <Tag color="gold" style={{ margin: 0 }}>真跑</Tag>}
                          {!isReal && <Tag style={{ margin: 0 }}>dry</Tag>}
                        </Space>
                        <span style={{ fontSize: 11, color: "#999" }}>
                          {r.style_no} · {r.elapsed_ms ? `${(r.elapsed_ms / 1000).toFixed(0)}s` : "?"}
                          {r.total_tokens_in ? ` · ${((r.total_tokens_in + (r.total_tokens_out || 0)) / 1000).toFixed(1)}k tok` : ""}
                        </span>
                      </div>
                    </Space>
                  </Space>
                </List.Item>
              );
            }}
          />
        )}
      </Card>

      <Card size="small" title={`测试夹具 (${fixtures?.length ?? 0})`}>
        {fixtures?.length ? (
          <Select
            style={{ width: "100%" }}
            placeholder="选夹具填充（todo）"
            disabled
            options={fixtures.map((f: any) => ({ value: f.id, label: f.name }))}
          />
        ) : (
          <div style={{ color: "#999", fontSize: 12 }}>暂无夹具——M5 实装</div>
        )}
      </Card>
    </Space>
  );
}
