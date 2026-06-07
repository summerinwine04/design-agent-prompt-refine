import { Card, List, Spin, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";

import { listPrompts } from "../api/client";

/**
 * Prompt 版本总览
 * 列出 5 个子节点，每个显示当前版本 + 历史版本。
 * 点击 → 跳到对应工作台抽屉打开编辑（todo）
 */
export default function PromptsPage() {
  const { data, isLoading } = useQuery({ queryKey: ["prompts"], queryFn: listPrompts });

  if (isLoading) return <Spin style={{ margin: 24 }} />;
  if (!data) return null;

  const items = Object.entries(data) as [string, any][];

  return (
    <div style={{ padding: 24, maxWidth: 880 }}>
      <Card title="Prompt 版本">
        <List
          itemLayout="vertical"
          dataSource={items}
          renderItem={([nodeId, info]) => (
            <List.Item key={nodeId}>
              <List.Item.Meta
                title={
                  <span>
                    <Tag color="blue">{nodeId}</Tag> {info.current_file}
                  </span>
                }
                description={
                  <div>
                    {info.versions.map((v: any) => (
                      <Tag key={v.version} color={v.version === "current" ? "green" : "default"}>
                        {v.version}
                      </Tag>
                    ))}
                  </div>
                }
              />
            </List.Item>
          )}
        />
      </Card>
    </div>
  );
}
