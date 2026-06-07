import { Empty } from "antd";

type Anchor = {
  面: "前身" | "后身" | "侧身";
  档: "A" | "B" | "C" | "D";
  位置中文: string;
  位置英文: string;
  尺寸: string;
};

type Plan = {
  方案编号: string;
  锚点列表: Anchor[];
};

const FACE_COLORS: Record<string, string> = {
  A: "#ff4d4f",
  B: "#1677ff",
  C: "#52c41a",
  D: "#fa8c16",
};

/**
 * 三面 SVG 人形示意图——把方案的锚点列表按 面 + 档 大致定位渲染成点。
 * 这是 Phase 1 的"看得过去"版本；Phase 2 可基于 placement_schema 的精确坐标做完整版。
 */
export default function FaceVisualizer({ plans }: { plans: Plan[] }) {
  if (!plans || plans.length === 0) return <Empty description="本节点无方案" />;

  return (
    <div>
      {plans.map((plan) => (
        <div key={plan.方案编号} style={{ marginBottom: 24 }}>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>{plan.方案编号}</div>
          <div style={{ display: "flex", gap: 16 }}>
            <FacePanel title="前身" face="前身" anchors={plan.锚点列表 ?? []} />
            <FacePanel title="后身" face="后身" anchors={plan.锚点列表 ?? []} />
            <FacePanel title="侧身" face="侧身" anchors={plan.锚点列表 ?? []} />
          </div>
        </div>
      ))}
    </div>
  );
}

function FacePanel({ title, face, anchors }: { title: string; face: Anchor["面"]; anchors: Anchor[] }) {
  const facing = anchors.filter((a) => a.面 === face);
  return (
    <div style={{ flex: 1, textAlign: "center" }}>
      <div style={{ fontSize: 12, marginBottom: 4 }}>{title}</div>
      <svg viewBox="0 0 100 140" width="100%" style={{ background: "#f6f7f9", border: "1px solid #e0e0e0", borderRadius: 4 }}>
        {/* 简化人形轮廓 */}
        <rect x="30" y="20" width="40" height="60" rx="3" fill="#fff" stroke="#999" />
        {/* 头 */}
        <circle cx="50" cy="12" r="8" fill="#fff" stroke="#999" />
        {/* 袖（仅侧身展示） */}
        {face === "侧身" && (
          <>
            <rect x="15" y="25" width="14" height="40" rx="2" fill="#fff" stroke="#999" />
            <rect x="71" y="25" width="14" height="40" rx="2" fill="#fff" stroke="#999" />
          </>
        )}
        {/* 锚点 */}
        {facing.map((a, i) => {
          // 极简启发：A 居中偏上，B 在胸/背区，C 散点，D 沿边
          const tierToY: Record<string, number> = { A: 38, B: 42, C: 60, D: 76 };
          const x = 35 + (i % 3) * 12;
          const y = tierToY[a.档] ?? 50;
          return (
            <g key={i}>
              <circle cx={x} cy={y} r={a.档 === "A" ? 7 : a.档 === "B" ? 5 : a.档 === "C" ? 3 : 2}
                fill={FACE_COLORS[a.档]} opacity={0.75} />
              <text x={x} y={y + 2} fontSize="3" fill="#fff" textAnchor="middle">{a.档}</text>
            </g>
          );
        })}
      </svg>
      <div style={{ fontSize: 10, color: "#888", marginTop: 4 }}>
        {facing.length === 0 ? "—" : facing.map((a) => `${a.档}:${a.位置中文}`).join(" / ")}
      </div>
    </div>
  );
}
