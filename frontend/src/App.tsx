import { Layout, Menu } from "antd";
import { Link, Route, Routes, useLocation } from "react-router-dom";

import WorkbenchPage from "./routes/WorkbenchPage";
import ComparePage from "./routes/ComparePage";
import PromptsPage from "./routes/PromptsPage";
import SettingsPage from "./routes/SettingsPage";
import TasksListPage from "./routes/TasksListPage";
import TaskNewPage from "./routes/TaskNewPage";
import TaskDetailPage from "./routes/TaskDetailPage";
import TasksComparePage from "./routes/TasksComparePage";

const { Header, Content } = Layout;

const NAV_ITEMS = [
  { key: "/", label: <Link to="/">工作台</Link> },
  { key: "/compare", label: <Link to="/compare">对比 Runs</Link> },
  { key: "/tasks", label: <Link to="/tasks">生图任务</Link> },
  { key: "/prompts", label: <Link to="/prompts">Prompt 版本</Link> },
  { key: "/settings", label: <Link to="/settings">设置</Link> },
];

export default function App() {
  const location = useLocation();
  const activeKey = "/" + (location.pathname.split("/")[1] || "");
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", background: "#fff", borderBottom: "1px solid #eee" }}>
        <div style={{ fontWeight: 600, marginRight: 32 }}>prompt-refine-agent</div>
        <Menu
          mode="horizontal"
          selectedKeys={[activeKey]}
          items={NAV_ITEMS}
          style={{ flex: 1, borderBottom: "none" }}
        />
      </Header>
      <Content style={{ padding: 0, background: "#f6f7f9" }}>
        <Routes>
          <Route path="/" element={<WorkbenchPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/tasks" element={<TasksListPage />} />
          <Route path="/tasks/new" element={<TaskNewPage />} />
          <Route path="/tasks/compare" element={<TasksComparePage />} />
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/prompts" element={<PromptsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </Content>
    </Layout>
  );
}
