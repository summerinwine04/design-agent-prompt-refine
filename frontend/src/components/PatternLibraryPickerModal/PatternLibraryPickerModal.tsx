/**
 * PatternLibraryPickerModal —— v6 图库输入源专用选图 Modal
 * (cache-bust: stale client.js removed 2026-07-12)
 *
 * 两步式流程：
 *   Step 1: 显示所有方向文件夹（可按母主题过滤），点选一个文件夹
 *   Step 2: 在选定文件夹里勾 1-3 张作为核心参考图（硬上限 3）
 *
 * 返回给 InputsPanel：{ folder_name: string, selected_files: string[] (1-3 张) }
 */

import { useMemo, useState } from "react";
import { Alert, Button, Empty, Image, Modal, Segmented, Space, Spin, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";

import { listPatternLibrary, PatternLibraryGroup } from "../../api/client";

type Props = {
  open: boolean;
  onCancel: () => void;
  onConfirm: (folder_name: string, selected_files: string[]) => void;
  // 初始状态（编辑已有选择时传入）
  initialFolder?: string;
  initialFiles?: string[];
};

const MAX_PICK = 3;

export default function PatternLibraryPickerModal({
  open, onCancel, onConfirm, initialFolder, initialFiles,
}: Props) {
  // step: 0 = 选文件夹；1 = 勾图
  const [step, setStep] = useState<0 | 1>(initialFolder ? 1 : 0);
  const [pickedFolder, setPickedFolder] = useState<string>(initialFolder || "");
  const [pickedFiles, setPickedFiles] = useState<Set<string>>(new Set(initialFiles || []));
  // 母主题过滤 tab
  const [categoryFilter, setCategoryFilter] = useState<string>("全部");
  // 放大预览
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

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

  const canConfirm = pickedFolder && pickedFiles.size >= 1 && pickedFiles.size <= MAX_PICK;

  const handleReset = () => {
    setStep(0);
    setPickedFolder("");
    setPickedFiles(new Set());
    setCategoryFilter("全部");
  };

  const handleClose = () => {
    onCancel();
    // 不 reset：下次打开时保留上次的选择
  };

  const handleConfirm = () => {
    if (!canConfirm) return;
    onConfirm(pickedFolder, Array.from(pickedFiles));
  };

  const togglePickFile = (fn: string) => {
    setPickedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(fn)) {
        next.delete(fn);
      } else {
        if (next.size >= MAX_PICK) {
          // 达到上限：不添加
          return prev;
        }
        next.add(fn);
      }
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
                  已选 <strong style={{ color: pickedFiles.size > 0 ? "#1677ff" : "#999" }}>
                    {pickedFiles.size}/{MAX_PICK}
                  </strong> 张
                </span>
              </>
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
              选定并返回 {canConfirm && `(${pickedFiles.size} 张)`}
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
              {filteredGroups.map((g) => (
                <div
                  key={g.folder_name}
                  onClick={() => { setPickedFolder(g.folder_name); setStep(1); }}
                  style={{
                    border: "1px solid #eee",
                    borderRadius: 6,
                    padding: 8,
                    cursor: "pointer",
                    transition: "border-color 0.15s, box-shadow 0.15s",
                  }}
                  onMouseEnter={(e) => {
                    (e.currentTarget as HTMLDivElement).style.borderColor = "#1677ff";
                    (e.currentTarget as HTMLDivElement).style.boxShadow = "0 2px 8px rgba(22,119,255,0.15)";
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLDivElement).style.borderColor = "#eee";
                    (e.currentTarget as HTMLDivElement).style.boxShadow = "";
                  }}
                >
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
              ))}
            </div>
          )}
        </>
      ) : (
        <>
          <div style={{ marginBottom: 12, fontSize: 13 }}>
            <Space>
              <span>已选文件夹：</span>
              <Tag color="blue">{activeGroup?.sub_topic || pickedFolder}</Tag>
              <span style={{ color: "#999", fontSize: 12 }}>
                共 {activeGroup?.image_count || 0} 张，勾 1-3 张作为核心参考图
              </span>
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
                const picked = pickedFiles.has(img.filename);
                const disabled = !picked && pickedFiles.size >= MAX_PICK;
                return (
                  <div
                    key={img.filename}
                    onClick={() => { if (!disabled) togglePickFile(img.filename); }}
                    style={{
                      position: "relative",
                      border: picked ? "3px solid #1677ff" : "1px solid #eee",
                      borderRadius: 4,
                      overflow: "hidden",
                      cursor: disabled ? "not-allowed" : "pointer",
                      opacity: disabled ? 0.5 : 1,
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
