import { Layout } from "antd";

import RunHeader from "../components/RunHeader/RunHeader";
import TraceTreePanel from "../components/TraceTree/TraceTreePanel";
import NodeDetailPanel from "../components/NodeDetail/NodeDetailPanel";
import InputsPanel from "../components/InputsPanel/InputsPanel";

const { Sider, Content } = Layout;

/**
 * 主工作台：单页三栏 + 顶部状态条
 *
 *   ┌─────────────────────────────────────────────────────────┐
 *   │  RunHeader（run_id / status / 进度 / token / 耗时）       │
 *   ├─────────┬──────────────────┬─────────────────────────────┤
 *   │ Inputs  │  Trace Tree      │  Node Detail                │
 *   │ (左 320)│  (中 1fr)        │  (右 1.4fr)                 │
 *   └─────────┴──────────────────┴─────────────────────────────┘
 */
export default function WorkbenchPage() {
  return (
    <Layout style={{ height: "calc(100vh - 64px)" }}>
      <Sider width={340} style={{ background: "#fff", padding: 16, overflowY: "auto" }}>
        <InputsPanel />
      </Sider>
      <Content style={{ display: "flex", flexDirection: "column" }}>
        <RunHeader />
        <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
          <div style={{ flex: 1, borderRight: "1px solid #eee", background: "#fff", overflowY: "auto" }}>
            <TraceTreePanel />
          </div>
          <div style={{ flex: 1.4, background: "#fff", overflowY: "auto" }}>
            <NodeDetailPanel />
          </div>
        </div>
      </Content>
    </Layout>
  );
}
