import { Layout, Menu } from "antd";
import { useQuery } from "@tanstack/react-query";
import { Link, Route, Routes, useLocation } from "react-router-dom";

import { getSettings } from "./api/client";

import WorkbenchPage from "./routes/WorkbenchPage";
import ComparePage from "./routes/ComparePage";
import PromptsPage from "./routes/PromptsPage";
import SettingsPage from "./routes/SettingsPage";
import TasksListPage from "./routes/TasksListPage";
import TaskNewPage from "./routes/TaskNewPage";
import TaskDetailPage from "./routes/TaskDetailPage";
import TasksComparePage from "./routes/TasksComparePage";
import TasksGalleryPage from "./routes/TasksGalleryPage";
import FittingRoomPage from "./routes/FittingRoomPage";
import SelectionCenterPage from "./routes/SelectionCenterPage";
import FixturesPage from "./routes/FixturesPage";
import BillingPage from "./routes/BillingPage";

const { Header, Content } = Layout;

const NAV_ITEMS = [
  { key: "/", label: <Link to="/">工作台</Link> },
  { key: "/compare", label: <Link to="/compare">对比 Runs</Link> },
  { key: "/tasks", label: <Link to="/tasks">设计生图任务</Link> },
  { key: "/selection", label: <Link to="/selection">🛒 选款中心</Link> },
  { key: "/fitting-room", label: <Link to="/fitting-room">👗 Fitting Room</Link> },
  { key: "/prompts", label: <Link to="/prompts">Prompt 版本</Link> },
  { key: "/fixtures", label: <Link to="/fixtures">测试夹具</Link> },
  { key: "/settings", label: <Link to="/settings">设置</Link> },
  { key: "/billing", label: <Link to="/billing">💰 账单</Link> },
];

// 访客模式下可见的导航（其余页面的 API 会被公网守卫 403）
const GUEST_NAV_KEYS = new Set(["/tasks", "/selection", "/fitting-room", "/billing"]);

export default function App() {
  const location = useLocation();
  const activeKey = "/" + (location.pathname.split("/")[1] || "");

  // 角色探测：设置接口只有本机/管理员可达；403 = 访客 → 导航收敛到三个 tab
  const { isError: isGuest } = useQuery({
    queryKey: ["access-probe"],
    queryFn: getSettings,
    retry: false,
    staleTime: Infinity,
  });
  const navItems = isGuest ? NAV_ITEMS.filter((i) => GUEST_NAV_KEYS.has(i.key)) : NAV_ITEMS;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{
        display: "flex", alignItems: "center", background: "#fff", borderBottom: "1px solid #eee",
        position: "sticky", top: 0, zIndex: 200,   // 滚动时导航钉在顶部
      }}>
        <div style={{ fontWeight: 600, marginRight: 32 }}>prompt-refine-agent</div>
        <Menu
          mode="horizontal"
          selectedKeys={[activeKey]}
          items={navItems}
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
          <Route path="/tasks/gallery" element={<TasksGalleryPage />} />
          <Route path="/billing" element={<BillingPage />} />
          <Route path="/selection" element={<SelectionCenterPage />} />
          <Route path="/fitting-room" element={<FittingRoomPage />} />
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/prompts" element={<PromptsPage />} />
          <Route path="/fixtures" element={<FixturesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </Content>
    </Layout>
  );
}
