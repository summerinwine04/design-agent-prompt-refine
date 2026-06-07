import { Button, Card, Form, Input, InputNumber, Spin, message } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getSettings, updateSettings } from "../api/client";

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["settings"], queryFn: getSettings });
  const mutation = useMutation({
    mutationFn: updateSettings,
    onSuccess: () => {
      message.success("已保存到 .env");
      queryClient.invalidateQueries({ queryKey: ["settings"] });
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
            default_max_tokens: data?.default_max_tokens,
            default_color_concurrency: data?.default_color_concurrency,
          }}
          onFinish={(values) => mutation.mutate(values)}
        >
          <Form.Item label="OpenAI API Key" name="openai_api_key" extra={data?.has_openai_key ? "已配置（不回显）" : "未配置"}>
            <Input.Password placeholder={data?.has_openai_key ? "已存（留空保持原值）" : "sk-..."} />
          </Form.Item>
          <Form.Item label="默认模型" name="default_model">
            <Input />
          </Form.Item>
          <Form.Item label="默认 max_tokens" name="default_max_tokens">
            <InputNumber min={1000} step={10000} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="默认颜色识别并发" name="default_color_concurrency">
            <InputNumber min={1} max={10} style={{ width: "100%" }} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={mutation.isPending}>保存</Button>
        </Form>
      </Card>
    </div>
  );
}
