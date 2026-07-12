import { Button, Card, Empty, Form, Input, InputNumber, Popconfirm, Space, Spin, Table, Tag, Tooltip, message } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { clearStyleCache, getSettings, getStyleCacheStats, updateSettings } from "../api/client";

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["settings"], queryFn: getSettings });
  const mutation = useMutation({
    mutationFn: updateSettings,
    onSuccess: () => {
      message.success("已保存到 .env");
      queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (err: any) => {
      // 静默失败是反人类的 — 把 backend 错误明确告知
      const detail = err?.response?.data?.detail || err?.message || String(err);
      const status = err?.response?.status;
      message.error(
        status ? `保存失败 (HTTP ${status})：${detail}` : `保存失败：${detail}`,
        8,
      );
      console.error("[settings] update failed:", err);
    },
  });

  if (isLoading) return <Spin style={{ margin: 24 }} />;

  return (
    <div style={{ padding: 24, maxWidth: 720 }}>
      <Card title="本地设置（.env）">
        <Form
          layout="vertical"
          initialValues={{
            default_model: data?.default_model,
            default_image_model: data?.default_image_model,
            default_max_tokens: data?.default_max_tokens,
            default_color_concurrency: data?.default_color_concurrency,
          }}
          onFinish={(values) => mutation.mutate(values)}
        >
          <Form.Item label="OpenAI API Key" name="openai_api_key" extra={data?.has_openai_key ? "已配置（不回显）" : "未配置"}>
            <Input.Password placeholder={data?.has_openai_key ? "已存（留空保持原值）" : "sk-..."} />
          </Form.Item>
          <Form.Item
            label="推理模型（文本 LLM，step1 解析 / step2 设计）"
            name="default_model"
            extra="常用：gpt-5.5 / gpt-4o / o4-mini 等"
          >
            <Input placeholder="gpt-5.5" />
          </Form.Item>
          <Form.Item
            label="生图模型（step3 图生图）"
            name="default_image_model"
            extra="常用：gpt-image-2（最新）/ gpt-image-1（旧）/ dall-e-3"
          >
            <Input placeholder="gpt-image-2" />
          </Form.Item>
          <Form.Item label="默认 max_tokens" name="default_max_tokens">
            <InputNumber min={1000} step={10000} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="默认颜色识别并发" name="default_color_concurrency">
            <InputNumber min={1} max={10} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item
            label="🌐 公网访问 · 管理员密码"
            name="public_admin_password"
            extra={data?.has_public_admin_password ? "已配置（留空保持原值）· 管理员 = 全页面全功能" : "未配置 · 管理员 = 全页面全功能"}
          >
            <Input.Password placeholder={data?.has_public_admin_password ? "已存（留空保持原值）" : "设置管理员访问密码..."} />
          </Form.Item>
          <Form.Item
            label="🌐 公网访问 · 访客密码"
            name="public_guest_password"
            extra={
              (data?.has_public_guest_password ? "已配置（留空保持原值）" : "未配置") +
              " · 访客只能用 Fitting Room / 生图任务 / 账单，无法发起设计和生图。两个密码都清空 = 关闭公网访问"
            }
          >
            <Input.Password placeholder={data?.has_public_guest_password ? "已存（留空保持原值）" : "设置访客访问密码..."} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={mutation.isPending}>保存</Button>
        </Form>
      </Card>

      <StyleCacheCard />
    </div>
  );
}


/**
 * 款级缓存管理：2.1 款式分析 / 2.2 颜色识别的跨 run 持久缓存。
 * 缓存 key 含 图片内容 md5 + prompt 内容 + 模型，换图/改 prompt/换模型会自动失效，
 * 这里的手动清除用于「想强制重新分析某个款」的场景。
 */
function StyleCacheCard() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["style-cache"],
    queryFn: getStyleCacheStats,
  });

  const clearMut = useMutation({
    mutationFn: (styleNo?: string) => clearStyleCache(styleNo),
    onSuccess: (res: any) => {
      message.success(`已清除 ${res.deleted_files} 个缓存条目`);
      queryClient.invalidateQueries({ queryKey: ["style-cache"] });
    },
    onError: (err: any) => {
      message.error("清除失败：" + (err?.response?.data?.detail || err?.message));
    },
  });

  const fmtBytes = (n: number) =>
    n > 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;

  const styles = data?.styles || [];

  return (
    <Card
      style={{ marginTop: 16 }}
      title={
        <Space>
          ♻️ 款级缓存（款式分析 / 颜色识别复用）
          <Tooltip title="同一个款在不同 run / 不同趋势 / 不同设计模式下复用 2.1 款式分析和 2.2 颜色识别结果。换款图、换色号图、改 2.1/2.2 prompt、换模型都会自动失效，通常不需要手动清。清除后下次 run 会重跑并自动回填。">
            <span style={{ color: "#999", fontSize: 12, cursor: "help" }}>?</span>
          </Tooltip>
        </Space>
      }
      extra={
        styles.length > 0 && (
          <Popconfirm
            title="清除全部款级缓存？"
            description="所有款下次 run 都会重跑 2.1 / 2.2（重新烧 token）"
            onConfirm={() => clearMut.mutate(undefined)}
            okText="清除全部"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button size="small" danger loading={clearMut.isPending}>
              清除全部
            </Button>
          </Popconfirm>
        )
      }
    >
      {isLoading ? (
        <Spin />
      ) : styles.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="还没有缓存条目——跑一次 run 后 2.1/2.2 的结果会自动存到这里"
        />
      ) : (
        <>
          <div style={{ fontSize: 12, color: "#888", marginBottom: 8 }}>
            共 {styles.length} 个款 · {fmtBytes(data!.total_bytes)} ·{" "}
            <span style={{ fontFamily: "ui-monospace, monospace" }}>{data!.root}</span>
          </div>
          <Table
            size="small"
            rowKey="style_no"
            pagination={styles.length > 10 ? { pageSize: 10 } : false}
            dataSource={styles}
            columns={[
              {
                title: "款号",
                dataIndex: "style_no",
                render: (v: string) => <span style={{ fontFamily: "ui-monospace, monospace" }}>{v}</span>,
              },
              {
                title: "缓存条目",
                render: (_: any, r: any) => (
                  <Space size={4}>
                    <Tag color="blue">款式分析 ×{r.analysis_count}</Tag>
                    <Tag color="cyan">颜色识别 ×{r.color_count}</Tag>
                  </Space>
                ),
              },
              {
                title: "大小",
                dataIndex: "total_bytes",
                width: 90,
                render: (v: number) => <span style={{ fontSize: 12 }}>{fmtBytes(v)}</span>,
              },
              {
                title: "最近更新",
                dataIndex: "updated_at",
                width: 150,
                render: (v: string) => <span style={{ fontSize: 12, color: "#888" }}>{v}</span>,
              },
              {
                title: "",
                width: 80,
                render: (_: any, r: any) => (
                  <Popconfirm
                    title={`清除 ${r.style_no} 的缓存？`}
                    description="该款下次 run 会重跑 2.1 / 2.2"
                    onConfirm={() => clearMut.mutate(r.style_no)}
                    okText="清除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                  >
                    <Button size="small" type="text" danger>
                      清除
                    </Button>
                  </Popconfirm>
                ),
              },
            ]}
          />
        </>
      )}
    </Card>
  );
}
