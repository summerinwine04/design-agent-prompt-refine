import {
  Drawer,
  Input,
  Button,
  Space,
  Tag,
  Select,
  Tooltip,
  Modal,
  message,
  Divider,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getPrompt,
  listPrompts,
  savePromptVersion,
  diffPrompts,
} from "../../api/client";
import { useRunStore } from "../../store/runStore";

/**
 * Prompt 编辑抽屉
 *
 * 工作流：
 *   1. NodeDetailPanel 点「⚙ 编辑 prompt」→ 用 nodeBizId 推导 step2 子节点编号（2.1 / 2.2 / ...）
 *   2. 拉当前版本的 system + user_template
 *   3. 用户编辑、切换版本、对比、保存
 *   4. 改完点「用此版本跑一次」→ 调 startRun 传 prompt_bundle.versions[bizId] = 新版本
 *
 * 设计选择：
 *   - 编辑器用 antd Input.TextArea（极简，没 syntax highlight）
 *   - {{变量}} 占位符提取后侧栏列出，标注是否在当前 user_template 中出现
 *   - 关闭时检测脏状态弹确认
 */
interface PromptDrawerProps {
  open: boolean;
  onClose: () => void;
  /** 节点的业务编号，如 "2.1" / "2.2" / "2.4"；非 LLM 节点为 null */
  nodeBizId: string | null;
  /** 节点名（仅用于 header 显示） */
  nodeName?: string;
}

// v4：6 个有 prompt 的子节点。其他业务编号（2.6 审计 / 2.7 决策）不允许编辑
const PROMPT_NODES = new Set(["2.1", "2.2", "2.3", "2.4", "2.5", "2.8"]);

export default function PromptDrawer({ open, onClose, nodeBizId, nodeName }: PromptDrawerProps) {
  const queryClient = useQueryClient();
  const currentRunId = useRunStore((s) => s.currentRunId);
  const startRun = useRunStore((s) => s.startRun);

  // 选中版本（current / v1 / v2 ...）
  const [selectedVersion, setSelectedVersion] = useState<string>("current");
  // 编辑中的内容
  const [systemDraft, setSystemDraft] = useState<string>("");
  const [userDraft, setUserDraft] = useState<string>("");
  // 用于检测脏状态
  const [systemOriginal, setSystemOriginal] = useState<string>("");
  const [userOriginal, setUserOriginal] = useState<string>("");

  // diff 模态框
  const [diffOpen, setDiffOpen] = useState(false);
  const [diffOtherVersion, setDiffOtherVersion] = useState<string>("current");

  const validNode = nodeBizId && PROMPT_NODES.has(nodeBizId);

  // 拉本节点的版本列表
  const { data: allPrompts } = useQuery({
    queryKey: ["prompts"],
    queryFn: listPrompts,
    enabled: open && !!validNode,
  });
  const versions = useMemo(() => {
    if (!allPrompts || !nodeBizId) return [];
    const nodeInfo = (allPrompts as any)[nodeBizId];
    return nodeInfo?.versions ?? [];
  }, [allPrompts, nodeBizId]);

  // 拉当前选中版本的内容
  const { data: promptData, isLoading } = useQuery({
    queryKey: ["prompt", nodeBizId, selectedVersion],
    queryFn: () => getPrompt(nodeBizId!, selectedVersion),
    enabled: open && !!validNode,
  });

  // 加载内容 → 写入编辑器 + 备份原值
  useEffect(() => {
    if (promptData) {
      const sys = promptData.system_prompt || "";
      const usr = promptData.user_template || "";
      setSystemDraft(sys);
      setUserDraft(usr);
      setSystemOriginal(sys);
      setUserOriginal(usr);
    }
  }, [promptData]);

  // 切版本时重置 selectedVersion
  useEffect(() => {
    if (open) setSelectedVersion("current");
  }, [open, nodeBizId]);

  // 脏状态检测
  const isDirty = systemDraft !== systemOriginal || userDraft !== userOriginal;

  // 提取所有 {{变量}}
  const placeholders = useMemo(() => {
    const matches = userDraft.match(/\{\{[^}]+\}\}/g) ?? [];
    return Array.from(new Set(matches));
  }, [userDraft]);

  // 推荐下一个版本号
  const nextVersion = useMemo(() => {
    const nums = versions
      .map((v: any) => v.version)
      .filter((v: string) => /^v\d+$/.test(v))
      .map((v: string) => Number(v.slice(1)));
    const max = nums.length ? Math.max(...nums) : 0;
    return `v${max + 1}`;
  }, [versions]);

  const handleClose = () => {
    if (isDirty) {
      Modal.confirm({
        title: "有未保存的修改",
        content: "确定关闭并丢弃修改？",
        okText: "丢弃修改",
        cancelText: "继续编辑",
        onOk: onClose,
      });
    } else {
      onClose();
    }
  };

  const handleSave = async () => {
    if (!nodeBizId) return;
    // 组装完整 .md 文件内容（保持 system + user template 二段式结构）
    const newContent = assembleMd(systemDraft, userDraft);
    try {
      await savePromptVersion(nodeBizId, {
        new_content: newContent,
        save_as: nextVersion,
      });
      message.success(`已存为 ${nextVersion}`);
      // 刷新版本列表，自动切到新版本
      await queryClient.invalidateQueries({ queryKey: ["prompts"] });
      setSelectedVersion(nextVersion);
      // 保存后原值刷新
      setSystemOriginal(systemDraft);
      setUserOriginal(userDraft);
    } catch (err: any) {
      message.error("保存失败：" + formatApiError(err));
      console.error("savePromptVersion error:", err);
    }
  };

  const handleRunWithThisVersion = () => {
    if (!nodeBizId) return;
    if (isDirty) {
      Modal.confirm({
        title: "有未保存的修改",
        content: `当前编辑器内容尚未保存。是否先存为 ${nextVersion} 并用 ${nextVersion} 跑？`,
        okText: `存 ${nextVersion} 再跑`,
        cancelText: "取消",
        onOk: async () => {
          await handleSave();
          triggerRun(nodeBizId, nextVersion);
        },
      });
    } else {
      triggerRun(nodeBizId, selectedVersion);
    }
  };

  const setPromptOverride = useRunStore((s) => s.setPromptOverride);
  const forkFromCurrent = useRunStore((s) => s.forkFromCurrent);

  // M4：如果当前有 currentRunId，"用此版本跑"会触发 Fork（复用缓存）
  // 否则只写入 overrides 让用户回 InputsPanel 触发完整跑
  const triggerRun = async (bizId: string, version: string) => {
    setPromptOverride(bizId, version);

    if (currentRunId) {
      // Fork 模式：复用源 run 缓存，仅重跑变化的节点 + 下游
      const overrides = { [bizId]: version };
      try {
        await forkFromCurrent(`node_${bizId}`, overrides);
        Modal.success({
          title: `⑂ Fork 已启动`,
          content: (
            <div>
              <p>源 run：<code>{currentRunId}</code></p>
              <p>改动：<code>{`${bizId} → ${version}`}</code></p>
              <p style={{ marginTop: 8 }}>
                因为只改了 <b>{bizId}</b>，源 run 中 <b>{bizId}</b> 及其下游节点会重跑，
                上游节点 ⚡ 缓存复用。
              </p>
              <p style={{ color: "#999", fontSize: 12, marginTop: 8 }}>
                trace 树中带「⚡缓存」字样的节点表示直接复用了源 run 的输出。
              </p>
            </div>
          ),
          onOk: onClose,
        });
      } catch (err: any) {
        Modal.error({
          title: "Fork 失败",
          content: err?.message || String(err),
        });
      }
    } else {
      // 没有源 run，只写 overrides 让用户回 InputsPanel
      Modal.success({
        title: `已记下：${bizId} 使用 ${version}`,
        content: (
          <div>
            <p>已写入 prompt overrides：<code>{`{${bizId}: ${version}}`}</code></p>
            <p>下次在左侧点「<b>空跑</b>」或「<b>真跑</b>」会自动用这个版本。</p>
            <p style={{ color: "#999", fontSize: 12, marginTop: 8 }}>
              提示：如果先跑一次 baseline，再点本按钮会触发 Fork 复用缓存，更快。
            </p>
          </div>
        ),
        onOk: onClose,
      });
    }
  };

  // 不是 LLM 节点：抽屉显示错误提示
  if (open && !validNode) {
    return (
      <Drawer
        open={open}
        onClose={onClose}
        title="无 prompt 可编辑"
        width={400}
      >
        <p>当前节点不是 LLM 节点（type=tool/decision/loop）——没有 prompt。</p>
      </Drawer>
    );
  }

  return (
    <Drawer
      open={open}
      onClose={handleClose}
      title={
        <Space>
          <span>⚙ Prompt 编辑</span>
          {nodeBizId && <Tag color="blue">{nodeBizId}</Tag>}
          {nodeName && <span style={{ fontSize: 13, color: "#666" }}>{nodeName}</span>}
        </Space>
      }
      width="60vw"
      destroyOnClose
      footer={
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <span style={{ color: "#999", fontSize: 12 }}>
            {isDirty ? "⬤ 未保存" : "✓ 已同步"}
          </span>
          <Space>
            <Button onClick={handleClose}>关闭</Button>
            <Tooltip title={isDirty ? `将创建新版本 ${nextVersion}` : "内容未改动，无需保存"}>
              <Button onClick={handleSave} disabled={!isDirty} type="default">
                💾 存为 {nextVersion}
              </Button>
            </Tooltip>
            <Button type="primary" onClick={handleRunWithThisVersion}>
              ▶ 用此版本跑一次
            </Button>
          </Space>
        </Space>
      }
    >
      {/* 头部：版本切换 + diff */}
      <Space style={{ marginBottom: 16, width: "100%" }} wrap>
        <span>当前版本：</span>
        <Select
          style={{ width: 180 }}
          value={selectedVersion}
          onChange={(v) => setSelectedVersion(v)}
          options={versions.map((v: any) => ({ value: v.version, label: v.version }))}
        />
        <Button
          size="small"
          onClick={() => {
            setDiffOtherVersion("current");
            setDiffOpen(true);
          }}
          disabled={versions.length < 2}
        >
          ⇄ 对比版本
        </Button>
        {promptData && (
          <span style={{ color: "#999", fontSize: 12, fontFamily: "ui-monospace, monospace" }}>
            {promptData.path}
          </span>
        )}
      </Space>

      <Divider style={{ margin: "8px 0" }}>SYSTEM PROMPT</Divider>
      <Input.TextArea
        value={systemDraft}
        onChange={(e) => setSystemDraft(e.target.value)}
        autoSize={{ minRows: 8, maxRows: 16 }}
        style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }}
        disabled={isLoading}
      />

      <Divider style={{ margin: "16px 0 8px" }}>USER PROMPT TEMPLATE</Divider>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 220px", gap: 12 }}>
        <Input.TextArea
          value={userDraft}
          onChange={(e) => setUserDraft(e.target.value)}
          autoSize={{ minRows: 10, maxRows: 24 }}
          style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }}
          disabled={isLoading}
        />
        <PlaceholderList placeholders={placeholders} />
      </div>

      {/* Diff Modal */}
      <Modal
        open={diffOpen}
        onCancel={() => setDiffOpen(false)}
        title={`Diff: ${nodeBizId}`}
        width="80vw"
        footer={null}
      >
        <Space style={{ marginBottom: 12 }}>
          <span>对比：</span>
          <Select
            style={{ width: 150 }}
            value={selectedVersion}
            onChange={(v) => setSelectedVersion(v)}
            options={versions.map((v: any) => ({ value: v.version, label: v.version }))}
          />
          <span>vs</span>
          <Select
            style={{ width: 150 }}
            value={diffOtherVersion}
            onChange={setDiffOtherVersion}
            options={versions.map((v: any) => ({ value: v.version, label: v.version }))}
          />
        </Space>
        <DiffView nodeBizId={nodeBizId!} v1={selectedVersion} v2={diffOtherVersion} />
      </Modal>
    </Drawer>
  );
}


/** 右侧侧栏：列出当前 user_template 中的 {{变量}} */
function PlaceholderList({ placeholders }: { placeholders: string[] }) {
  return (
    <div
      style={{
        background: "#fafafa",
        border: "1px solid #eee",
        borderRadius: 4,
        padding: 12,
        fontSize: 12,
        overflowY: "auto",
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 8 }}>
        模板占位符 ({placeholders.length})
      </div>
      {placeholders.length === 0 ? (
        <div style={{ color: "#999" }}>暂无 {`{{变量}}`} 占位符</div>
      ) : (
        placeholders.map((p) => (
          <div
            key={p}
            style={{
              fontFamily: "ui-monospace, monospace",
              color: "#1677ff",
              marginBottom: 4,
            }}
          >
            {p}
          </div>
        ))
      )}
      <Divider style={{ margin: "12px 0 8px" }} />
      <div style={{ color: "#999", lineHeight: 1.5 }}>
        每次运行时这些占位符会被实际值替换。<br />
        模板里加新占位符 → 必须在 orchestrator/nodes.py 里也加对应的变量映射。
      </div>
    </div>
  );
}


/** 两版本 unified diff */
function DiffView({ nodeBizId, v1, v2 }: { nodeBizId: string; v1: string; v2: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["prompt-diff", nodeBizId, v1, v2],
    queryFn: () => diffPrompts(nodeBizId, v1, v2),
  });
  if (isLoading) return <div>计算 diff 中...</div>;
  const diffText = (data as any)?.diff ?? "";
  if (!diffText.trim()) {
    return <div style={{ color: "#52c41a" }}>两版本完全一致 ✓</div>;
  }
  return (
    <pre
      style={{
        fontFamily: "ui-monospace, monospace",
        fontSize: 12,
        background: "#1e1e1e",
        color: "#d4d4d4",
        padding: 12,
        borderRadius: 4,
        maxHeight: "60vh",
        overflow: "auto",
      }}
    >
      {colorizeDiff(diffText)}
    </pre>
  );
}


/** 给 unified diff 上颜色（+ 绿 / - 红 / @@ 蓝） */
function colorizeDiff(diff: string): React.ReactNode {
  return diff.split("\n").map((line, i) => {
    let color = "#d4d4d4";
    if (line.startsWith("+++") || line.startsWith("---")) color = "#888";
    else if (line.startsWith("+")) color = "#a3d977";
    else if (line.startsWith("-")) color = "#ff7373";
    else if (line.startsWith("@@")) color = "#5fcfff";
    return (
      <div key={i} style={{ color }}>
        {line || " "}
      </div>
    );
  });
}


/** 把 system + user template 重新组装成 .md 文件内容（与 orchestrator/prompts.py load_prompt_file 解析逻辑对应） */
function assembleMd(system: string, userTemplate: string): string {
  return `## ═══ SYSTEM PROMPT ═══

${system}

---

## ═══ USER PROMPT TEMPLATE ═══

${userTemplate}
`;
}


/** 把 axios/FastAPI 错误统一格式化为人类可读字符串。
 *  支持：纯 string、{detail: string}、{detail: [{msg, loc}, ...]}、Error.message
 */
function formatApiError(err: any): string {
  if (!err) return "未知错误";
  const detail = err?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: any) => {
        const loc = Array.isArray(d.loc) ? d.loc.join(".") : "";
        return `${loc ? `[${loc}] ` : ""}${d.msg || JSON.stringify(d)}`;
      })
      .join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return err?.message || String(err);
}
