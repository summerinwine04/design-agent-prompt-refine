import { Card, Empty, Spin, Table, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";

import { listFixtures } from "../api/client";

/**
 * 测试夹具列表 + 横向对比视图
 * 阶段一：列表 + 新建。横向对比放阶段二。
 */
export default function FixturesPage() {
  const { data, isLoading } = useQuery({ queryKey: ["fixtures"], queryFn: listFixtures });

  if (isLoading) return <Spin style={{ margin: 24 }} />;

  return (
    <div style={{ padding: 24 }}>
      <Card title="测试夹具" extra={<a>+ 新建夹具（todo）</a>}>
        {!data || data.length === 0 ? (
          <Empty description="还没有夹具。点新建从当前工作台快照一组冻结输入。" />
        ) : (
          <Table
            dataSource={data}
            rowKey="id"
            pagination={false}
            columns={[
              { title: "名称", dataIndex: "name" },
              { title: "趋势", dataIndex: "trend_json_path", ellipsis: true },
              { title: "款号", dataIndex: "ref_image_path", ellipsis: true },
              { title: "性别比", dataIndex: "gender_ratio", render: (v) => <Tag>{v}</Tag> },
              { title: "K", dataIndex: "num_designs_k", width: 60 },
              { title: "色号子集", dataIndex: "selected_colors", render: (v: string[] | null) => v ? v.join(",") : "全部" },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
