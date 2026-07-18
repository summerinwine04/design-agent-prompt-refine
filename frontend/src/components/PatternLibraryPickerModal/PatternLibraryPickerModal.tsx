/**
 * PatternLibraryPickerModal —— 图库输入源选图 Modal
 *
 * 两种模式：
 *   单文件夹模式（v6，强单主题/成套 CONVERGE 用）：
 *     Step 1: 选一个方向文件夹 → Step 2: 勾 ≥1 张核心参考图（不设上限）
 *     确认回调 onConfirm(folder_name, selected_files)
 *   多文件夹模式（v7，多主题 × 图库 用，multiFolder=true）：
 *     购物车式：可进出多个文件夹，各勾任意张（每个入选文件夹 = 一个设计方向）
 *     确认回调 onConfirmMulti(selections: [{folder, files}])
 */

import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Empty, Image, Modal, Segmented, Space, Spin, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";

import { listPatternLibrary, PatternLibraryGroup } from "../../api/client";

export type PatternLibrarySelection = { folder: string; files: string[] };

type Props = {
  open: boolean;
  onCancel: () => void;
  // —— 单文件夹模式（multiFolder 不传 / false）——
  onConfirm?: (folder_name: string, selected_files: string[]) => void;
  initialFolder?: string;
  initialFiles?: string[];
  // —— v7 多文件夹模式 ——
  multiFolder?: boolean;
  onConfirmMulti?: (selections: PatternLibrarySelection[]) => void;
  initialSelections?: PatternLibrarySelection[];
};

export default function PatternLibraryPickerModal({
  open, onCancel, onConfirm, initialFolder, initialFiles,
  multiFolder = false, onConfirmMulti, initialSelections,
}: Props) {
  // step: 0 = 选文件夹；1 = 勾图
  const [step, setStep] = useState<0 | 1>(!multiFolder && initialFolder ? 1 : 0);
  // 当前正在浏览的文件夹（两种模式共用）
  const [pickedFolder, setPickedFolder] = useState<string>(initialFolder || "");
  // 购物车：folder → 勾选的文件名集合（单文件夹模式下只会有一个 key）
  const [cart, setCart] = useState<Map<string, Set<string>>>(() => {
    const m = new Map<string, Set<string>>();
    if (multiFolder && initialSelections) {
      for (const s of initialSelections) m.set(s.folder, new Set(s.files));
    } else if (initialFolder && initialFiles?.length) {
      m.set(initialFolder, new Set(initialFiles));
    }
    return m;
  });
  // 母主题过滤 tab
  const [categoryFilter, setCategoryFilter] = useState<string>("全部");
  // 放大预览
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  // 每次打开都按当前模式从「外部已确认的选择」同步——
  // 单实例 Modal 被多主题（多文件夹）/ 收敛（单文件夹）两种模式复用，
  // 若只在购物车为空时恢复，跨模式开关后会显示陈旧内容（看起来像选择被清了）。
  // 取消不确认 = 丢弃本次改动，回到上次确认的状态（标准语义）。
  useEffect(() => {
    if (!open) return;
    const m = new Map<string, Set<string>>();
    if (multiFolder) {
      for (const s of initialSelections || []) m.set(s.folder, new Set(s.files));
      setStep(0);
      setPickedFolder("");
    } else {
      if (initialFolder && initialFiles?.length) m.set(initialFolder, new Set(initialFiles));
      setPickedFolder(initialFolder || "");
      setStep(initialFolder ? 1 : 0);
    }
    setCart(m);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, multiFolder]);

  const { data, isLoading } = useQuery({
    queryKey: ["pattern-library"],
    queryFn: () => listPatternLibrary(),
    enabled: open,
  });

  const groups: PatternLibraryGroup[] = data?.groups || [];
  const categories = ["全部", ...(data?.categories || [])];

  const filteredGroups = useMemo(() => {
    if (categoryFilter === "全部") return groups;
    return groups.filter((g) => g.parent_topic === categoryFilter);
  }, [groups, categoryFilter]);

  const activeGroup = useMemo(
    () => groups.find((g) => g.folder_name === pickedFolder) || null,
    [groups, pickedFolder],
  );

  const activeFiles: Set<string> = cart.get(pickedFolder) || new Set();

  // 汇总：入选的文件夹（≥1 张）
  const selections: PatternLibrarySelection[] = useMemo(() => {
    const out: PatternLibrarySelection[] = [];
    for (const [folder, files] of cart.entries()) {
      if (files.size > 0) out.push({ folder, files: Array.from(files) });
    }
    return out;
  }, [cart]);
  const totalPicked = selections.reduce((n, s) => n + s.files.length, 0);

  const canConfirm = multiFolder
    ? selections.length >= 1
    : !!pickedFolder && activeFiles.size >= 1;

  const handleReset = () => {
    setStep(0);
    setPickedFolder("");
    setCart(new Map());
    setCategoryFilter("全部");
  };

  const handleClose = () => {
    onCancel();
    // 不 reset：下次打开时保留上次的选择
  };

  const handleConfirm = () => {
    if (!canConfirm) return;
    if (multiFolder) {
      onConfirmMulti?.(selections);
    } else {
      onConfirm?.(pickedFolder, Array.from(activeFiles));
    }
  };

  const enterFolder = (folder: string) => {
    if (!multiFolder && pickedFolder && folder !== pickedFolder) {
      // 单文件夹模式：换文件夹 = 丢弃旧文件夹的勾选
      setCart(new Map());
    }
    setPickedFolder(folder);
    setStep(1);
  };

  const togglePickFile = (fn: string) => {
    setCart((prev) => {
      const next = new Map(prev);
      const files = new Set(next.get(pickedFolder) || []);
      if (files.has(fn)) files.delete(fn); else files.add(fn);
      next.set(pickedFolder, files);
      return next;
    });
  };

  const pickAllInFolder = () => {
    if (!activeGroup) return;
    setCart((prev) => {
      const next = new Map(prev);
      next.set(pickedFolder, new Set(activeGroup.images.map((i) => i.filename)));
      return next;
    });
  };

  const clearFolder = () => {
    setCart((prev) => {
      const next = new Map(prev);
      next.delete(pickedFolder);
      return next;
    });
  };

  return (
    <Modal
      open={open}
      onCancel={handleClose}
      width={"80vw"}
      title={
        <Space>
          🖼 印花图案库选图
          {multiFolder && <Tag color="purple" style={{ margin: 0 }}>多方向模式：每个文件夹 = 一个设计方向</Tag>}
          {data?.meta && (
            <span style={{ fontSize: 12, color: "#999" }}>
              {data.meta.group_count} 组 · {data.meta.image_count} 张
            </span>
          )}
        </Space>
      }
      footer={
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <Space>
            {step === 1 && (
              <>
                <Button size="small" onClick={() => { setStep(0); }}>← 返回选文件夹</Button>
                <span style={{ fontSize: 12, color: "#666" }}>
                  本夹已勾 <strong style={{ color: activeFiles.size > 0 ? "#1677ff" : "#999" }}>
                    {activeFiles.size}
                  </strong> 张
                </span>
              </>
            )}
            {multiFolder && (
              <span style={{ fontSize: 12, color: "#666" }}>
                共 <strong style={{ color: selections.length > 0 ? "#722ed1" : "#999" }}>{selections.length}</strong> 个方向
                · <strong>{totalPicked}</strong> 张
              </span>
            )}
          </Space>
          <Space>
            <Button size="small" onClick={handleReset}>清空重选</Button>
            <Button onClick={handleClose}>取消</Button>
            <Button
              type="primary"
              disabled={!canConfirm}
              onClick={handleConfirm}
            >
              {multiFolder
                ? `选定并返回${canConfirm ? ` (${selections.length} 方向 ${totalPicked} 张)` : ""}`
                : `选定并返回${canConfirm ? ` (${activeFiles.size} 张)` : ""}`}
            </Button>
          </Space>
        </Space>
      }
    >
      {isLoading ? (
        <Spin tip="加载图库..." style={{ padding: 32, display: "block", textAlign: "center" }} />
      ) : !data?.available ? (
        <Alert
          type="warning"
          message="图库不可用"
          description={data?.error || "PATTERN_LIBRARY_ROOT 目录不存在或为空。在 .env 中配置后重启 backend"}
        />
      ) : step === 0 ? (
        <>
          <div style={{ marginBottom: 12 }}>
            <Segmented
              size="small"
              value={categoryFilter}
              onChange={(v) => setCategoryFilter(String(v))}
              options={categories.map((c) => ({
                label: c === "全部" ? `全部 ${groups.length}` : c,
                value: c,
              }))}
            />
          </div>
          {filteredGroups.length === 0 ? (
            <Empty description="当前分类下没有方向文件夹" />
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
                gap: 12,
                maxHeight: "60vh",
                overflow: "auto",
                padding: 2,
              }}
            >
              {filteredGroups.map((g) => {
                const nPicked = cart.get(g.folder_name)?.size || 0;
                return (
                  <div
                    key={g.folder_name}
                    onClick={() => enterFolder(g.folder_name)}
                    style={{
                      border: nPicked > 0 ? "2px solid #722ed1" : "1px solid #eee",
                      borderRadius: 6,
                      padding: 8,
                      cursor: "pointer",
                      transition: "border-color 0.15s, box-shadow 0.15s",
                      position: "relative",
                    }}
                    onMouseEnter={(e) => {
                      (e.currentTarget as HTMLDivElement).style.boxShadow = "0 2px 8px rgba(22,119,255,0.15)";
                    }}
                    onMouseLeave={(e) => {
                      (e.currentTarget as HTMLDivElement).style.boxShadow = "";
                    }}
                  >
                    {nPicked > 0 && (
                      <div style={{
                        position: "absolute", top: 4, right: 4, zIndex: 2,
                        background: "#722ed1", color: "#fff",
                        fontSize: 11, fontWeight: 600,
                        padding: "1px 8px", borderRadius: 10,
                      }}>
                        已选 {nPicked}
                      </div>
                    )}
                    <div style={{
                      width: "100%",
                      aspectRatio: "1/1",
                      background: "#fafafa",
                      borderRadius: 4,
                      overflow: "hidden",
                      marginBottom: 6,
                    }}>
                      <img
                        src={g.cover_url}
                        alt={g.folder_name}
                        loading="lazy"
                        style={{ width: "100%", height: "100%", objectFit: "cover" }}
                      />
                    </div>
                    <div style={{ fontSize: 12, fontWeight: 600 }}>{g.sub_topic}</div>
                    <div style={{ fontSize: 10, color: "#999", marginTop: 2 }}>
                      <Tag style={{ margin: 0, marginRight: 4 }}>{g.parent_topic}</Tag>
                      {g.image_count} 图
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      ) : (
        <>
          <div style={{ marginBottom: 12, fontSize: 13 }}>
            <Space>
              <span>{multiFolder ? "当前方向：" : "已选文件夹："}</span>
              <Tag color={multiFolder ? "purple" : "blue"}>{activeGroup?.sub_topic || pickedFolder}</Tag>
              <span style={{ color: "#999", fontSize: 12 }}>
                共 {activeGroup?.image_count || 0} 张，勾 ≥1 张{multiFolder ? "（该文件夹即成为一个设计方向）" : "作为核心参考图"}
              </span>
              <Button size="small" onClick={pickAllInFolder}>全选本夹</Button>
              {activeFiles.size > 0 && <Button size="small" onClick={clearFolder}>清空本夹</Button>}
            </Space>
          </div>
          {!activeGroup || activeGroup.images.length === 0 ? (
            <Empty description="该文件夹为空" />
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
                gap: 8,
                maxHeight: "60vh",
                overflow: "auto",
                padding: 2,
              }}
            >
              {activeGroup.images.map((img) => {
                const picked = activeFiles.has(img.filename);
                return (
                  <div
                    key={img.filename}
                    onClick={() => togglePickFile(img.filename)}
                    style={{
                      position: "relative",
                      border: picked ? "3px solid #1677ff" : "1px solid #eee",
                      borderRadius: 4,
                      overflow: "hidden",
                      cursor: "pointer",
                      background: "#fafafa",
                      aspectRatio: "1/1",
                    }}
                  >
                    <img
                      src={img.url}
                      alt={img.filename}
                      loading="lazy"
                      style={{ width: "100%", height: "100%", objectFit: "cover" }}
                    />
                    {/* 🔍 放大预览按钮 */}
                    <div
                      onClick={(e) => { e.stopPropagation(); setPreviewUrl(img.url); }}
                      onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(0,0,0,0.85)"; }}
                      onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(0,0,0,0.5)"; }}
                      title="放大预览"
                      style={{
                        position: "absolute", top: 4, left: 4,
                        width: 22, height: 22, borderRadius: 4,
                        background: "rgba(0,0,0,0.5)", color: "#fff",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 12, cursor: "zoom-in", zIndex: 2,
                      }}
                    >🔍</div>
                    {/* 选中角标 */}
                    {picked && (
                      <div style={{
                        position: "absolute", top: 4, right: 4,
                        width: 22, height: 22, borderRadius: 11,
                        background: "#1677ff", color: "#fff",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 12, fontWeight: 700, zIndex: 2,
                      }}>✓</div>
                    )}
                    {/* 底部文件名 */}
                    <div style={{
                      position: "absolute", bottom: 0, left: 0, right: 0,
                      padding: "3px 6px",
                      background: "linear-gradient(transparent, rgba(0,0,0,0.7))",
                      color: "#fff", fontSize: 10,
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {img.filename.replace(/\.[^.]+$/, "")}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* 隐藏的 antd Image，用于全屏预览浮层 */}
          {previewUrl && (
            <Image
              src={previewUrl}
              wrapperStyle={{ display: "none" }}
              preview={{
                visible: !!previewUrl,
                src: previewUrl,
                onVisibleChange: (v) => { if (!v) setPreviewUrl(null); },
              }}
            />
          )}
        </>
      )}
    </Modal>
  );
}
