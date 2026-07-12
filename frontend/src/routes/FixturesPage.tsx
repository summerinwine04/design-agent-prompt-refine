import { Card, Empty, Spin, Table, Tag, Button, Space, Modal, message, Alert, Segmented } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, Link } from "react-router-dom";
import { useMemo, useState } from "react";

import { listFixtures, deleteFixture } from "../api/client";

const MODE_LABEL: Record<string, string> = {
  MULTI_TOPIC: "🎨 多主题",
  SINGLE_TOPIC_STRONG: "🧬 强单主题",
  COLLECTION_2SKU: "👕👖 上下装",
};
const MODE_COLOR: Record<string, string> = {
  MULTI_TOPIC: "default",
  SINGLE_TOPIC_STRONG: "purple",
  COLLECTION_2SKU: "gold",
};

/**
 * 测试夹具集中管理页
 *
 * 创建：不在此页做（去工作台 InputsPanel 点「💾 存为夹具」存）
 * 应用：表行点「✓ 应用」→ 跳工作台 + ?fixture=<id> 自动填表
 * 删除：表行点「× 删除」→ Modal 确认 → DELETE /fixtures/{id}
 */
export default function FixturesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["fixtures"], queryFn: listFixtures });

  const delMut = useMutation({
    mutationFn: (id: string) => deleteFixture(id),
    onSuccess: () => {
      message.success("已删除");
      queryClient.invalidateQueries({ queryKey: ["fixtures"] });
    },
    onError: (err: any) => {
      message.error("删除失败：" + (err?.response?.data?.detail || err?.message));
    },
  });

  const [modeFilter, setModeFilter] = useState<string>("ALL");
  const filteredData = useMemo(() => {
    if (!data) return [];
    if (modeFilter === "ALL") return data;
    return data.filter((r: any) => (r.design_mode || "MULTI_TOPIC") === modeFilter);
  }, [data, modeFilter]);

  // 按 mode 分组统计（用于 tab 徽章数字）
  const countByMode = useMemo(() => {
    const c: Record<string, number> = { ALL: 0, MULTI_TOPIC: 0, SINGLE_TOPIC_STRONG: 0, COLLECTION_2SKU: 0 };
    (data || []).forEach((r: any) => {
      c.ALL += 1;
      const m = r.design_mode || "MULTI_TOPIC";
      c[m] = (c[m] || 0) + 1;
    });
    return c;
  }, [data]);

  if (isLoading) return <Spin style={{ margin: 24 }} />;

  return (
    <div style={{ padding: 24 }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="夹具 = 一组冻结的输入快照（趋势/图库 + 款号 + 性别比 + K），可反复跑不同 PromptBundle 做横向对比"
        description={
          <span style={{ fontSize: 12 }}>
            新建：在 <Link to="/">工作台</Link> 填好表单后点「💾 存为夹具」。
            应用：点表行的「✓ 应用」会跳回工作台并自动填表。
          </span>
        }
      />

      <div style={{ marginBottom: 12 }}>
        <Segmented
          value={modeFilter}
          onChange={(v) => setModeFilter(String(v))}
          options={[
            { label: `全部 ${countByMode.ALL}`, value: "ALL" },
            { label: `🎨 多主题 ${countByMode.MULTI_TOPIC}`, value: "MULTI_TOPIC" },
            { label: `🧬 强单主题 ${countByMode.SINGLE_TOPIC_STRONG}`, value: "SINGLE_TOPIC_STRONG" },
            { label: `👕👖 上下装 ${countByMode.COLLECTION_2SKU}`, value: "COLLECTION_2SKU" },
          ]}
        />
      </div>

      <Card title={`测试夹具 (${filteredData.length} / 共 ${data?.length ?? 0})`}>
        {!filteredData || filteredData.length === 0 ? (
          <Empty
            description={
              <span style={{ fontSize: 13 }}>
                还没有夹具——去 <Link to="/">工作台</Link> 把一组调通的输入存下来
              </span>
            }
          />
        ) : (
          <Table
            dataSource={filteredData}
            rowKey="id"
            pagination={false}
            size="small"
            columns={[
              {
                title: "名称",
                dataIndex: "name",
                render: (v: string, row: any) => (
                  <Space direction="vertical" size={0}>
                    <strong>{v}</strong>
                    {row.description && <span style={{ fontSize: 11, color: "#999" }}>{row.description}</span>}
                  </Space>
                ),
              },
              {
                title: "模式",
                dataIndex: "design_mode",
                width: 130,
                render: (v: string) => {
                  const m = v || "MULTI_TOPIC";
                  return <Tag color={MODE_COLOR[m] || "default"}>{MODE_LABEL[m] || m}</Tag>;
                },
              },
              {
                title: "输入源",
                dataIndex: "input_source",
                width: 100,
                render: (v: string) => {
                  const s = v || "trend_report";
                  return s === "pattern_library"
                    ? <Tag color="cyan">🖼 图库</Tag>
                    : <Tag>📄 趋势</Tag>;
                },
              },
              {
                title: "趋势 / 图库",
                width: 200,
                ellipsis: true,
                render: (_: any, row: any) => {
                  if ((row.input_source || "trend_report") === "pattern_library") {
                    return <code style={{ fontSize: 11 }}>{row.pattern_library_path || "-"}</code>;
                  }
                  return <code style={{ fontSize: 11 }}>{row.trend_json_path?.split(/[\\/]/).pop() || "-"}</code>;
                },
              },
              {
                title: "款号",
                dataIndex: "ref_image_path",
                width: 130,
                render: (v: string) => (
                  <code style={{ fontSize: 11 }}>
                    {v?.split(/[\\/]/).pop()?.replace(/\.\w+$/, "")}
                  </code>
                ),
              },
              { title: "性别比", dataIndex: "gender_ratio", width: 110, render: (v) => <Tag>{v}</Tag> },
              { title: "K", dataIndex: "num_designs_k", width: 50 },
              {
                title: "色号子集",
                dataIndex: "selected_colors",
                width: 100,
                render: (v: string[] | null) => (v?.length ? <Tag>{v.length} 色</Tag> : <Tag color="default">全部</Tag>),
              },
              {
                title: "创建于",
                dataIndex: "created_at",
                width: 150,
                render: (v: string) => <span style={{ fontSize: 11, color: "#999" }}>{v}</span>,
              },
              {
                title: "操作",
                width: 160,
                render: (_: any, row: any) => (
                  <Space size={4}>
                    <Button
                      type="primary"
                      size="small"
                      onClick={() => navigate(`/?fixture=${row.id}`)}
                    >
                      ✓ 应用
                    </Button>
                    <Button
                      danger
                      size="small"
                      onClick={() =>
                        Modal.confirm({
                          title: `删除夹具：${row.name}？`,
                          icon: null,
                          content: "只删 fixture 记录本身，不影响已经用它跑过的 run。",
                          okText: "删除",
                          okType: "danger",
                          cancelText: "取消",
                          onOk: () => delMut.mutate(row.id),
                        })
                      }
                    >
                      × 删除
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
