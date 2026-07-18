import { Card, Tag, Space, Spin, Empty, Button, Image, Modal, Pagination, Select, Input, message, Switch } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";

import {
  listAllImages, listImageCategories, bulkUpsertImageCategories,
  addSelection, listSelectionImageIds,
} from "../api/client";

// 款号中文名启发式：服/衣/衫→上装，裤/裙→下装，其他默认上装
function guessCategory(styleNo: string | undefined): "top" | "bottom" {
  if (!styleNo) return "top";
  if (/[裤裙]/.test(styleNo)) return "bottom";
  if (/[服衣衫]/.test(styleNo)) return "top";
  return "top";
}

// 注：组套已搬到选款中心（漏斗：生图候选 → 选款入仓 → 选款中心组套 → Fitting Room）。
// Gallery 的多选 + 上/下装归类现在服务于「批量选款入仓」。

/**
 * 跨任务全局图片墙（Gallery）
 *
 * 数据源：GET /tasks/all-images，返回所有成功生成的图（扁平列表）。
 * 主要交互：
 *   - 顶部筛选（按款号 / 趋势 / task_id 子串搜索）
 *   - 平铺墙：成功的图按 task_created_at 倒序排列
 *   - 点图：弹大图 + 完整元信息（来自哪个 task、色号、主题、prompt）
 */
export default function TasksGalleryPage() {
  const [filterStyle, setFilterStyle] = useState<string | null>(null);
  const [filterTrend, setFilterTrend] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [modalItem, setModalItem] = useState<any>(null);
  // 全屏放大预览的 URL —— 用 antd Image 的浮层，跟 modalItem 完全解耦
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  // 组套模式
  const [composeMode, setComposeMode] = useState(searchParams.get("compose") === "1");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);          // 有序选择列表
  const [localCatOverrides, setLocalCatOverrides] = useState<Map<string, "top" | "bottom">>(new Map());

  // URL param 变化时同步
  useEffect(() => {
    setComposeMode(searchParams.get("compose") === "1");
  }, [searchParams]);

  const { data, isLoading, error } = useQuery({
    queryKey: ["all-images"],
    queryFn: () => listAllImages(1000),
    refetchInterval: composeMode ? false : 5000,     // 组套时暂停刷新，防止 grid 抖动
  });

  // 服务端持久化的归类映射
  const { data: dbCategories } = useQuery({
    queryKey: ["image-categories"],
    queryFn: () => listImageCategories(),
    enabled: composeMode,
  });

  // 合并：本地临时覆盖 优先于 服务端映射
  const categoryMap = useMemo(() => {
    const m = new Map<string, "top" | "bottom">();
    if (dbCategories) Object.entries(dbCategories).forEach(([k, v]) => m.set(k, v as any));
    localCatOverrides.forEach((v, k) => m.set(k, v));
    return m;
  }, [dbCategories, localCatOverrides]);

  const getCategoryForImage = (item: any): "top" | "bottom" => {
    const id = `${item.task_id}:${item.plan_id}`;
    // 优先级：用户手动归类 > 方案级 role（成套 run 精确款位）> 款号中文启发式（老数据兜底）。
    // 成套 run 的 style_no 是 "款A+款B" 组合串，含「裤」字会把上装也误判成下装——必须用 role。
    if (categoryMap.has(id)) return categoryMap.get(id)!;
    if (item.role === "top" || item.role === "bottom") return item.role;
    return guessCategory(item.style_no);
  };

  // 用户点上装/下装标签切换
  const catSaveMut = useMutation({
    mutationFn: (payload: { image_id: string; category: "top" | "bottom" }) =>
      bulkUpsertImageCategories([{ image_id: payload.image_id, category: payload.category, source: "manual" }]),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["image-categories"] }),
  });

  const toggleCategory = (imageId: string, current: "top" | "bottom") => {
    const next: "top" | "bottom" = current === "top" ? "bottom" : "top";
    const nextMap = new Map(localCatOverrides);
    nextMap.set(imageId, next);
    setLocalCatOverrides(nextMap);
    catSaveMut.mutate({ image_id: imageId, category: next });
  };

  const toggleSelection = (imageId: string) => {
    setSelectedIds((prev) => {
      if (prev.includes(imageId)) return prev.filter((x) => x !== imageId);
      return [...prev, imageId];
    });
  };

  // 已入仓的 image_id 集合（角标 + 防重复入仓提示）
  const { data: stockIds } = useQuery({
    queryKey: ["selection-image-ids"],
    queryFn: () => listSelectionImageIds(),
  });
  const inStock = useMemo(() => new Set(stockIds || []), [stockIds]);

  // 批量选款入仓
  const addSelMut = useMutation({
    mutationFn: () => {
      const itemsById = new Map(
        (data?.items || []).map((it: any) => [`${it.task_id}:${it.plan_id}`, it]),
      );
      return addSelection(
        selectedIds.map((id) => {
          const it: any = itemsById.get(id) || {};
          return {
            image_id: id,
            category: categoryMap.get(id)
              || (it.role === "top" || it.role === "bottom" ? it.role : guessCategory(it.style_no)),
            style_no: it.style_no || null,
            color_code: it.color_code || null,
            color_name: it.color_name || null,
          };
        }),
      );
    },
    onSuccess: (r: any) => {
      message.success(
        `已入仓 ${r.added} 款` + (r.skipped ? `（${r.skipped} 款已在仓，跳过）` : ""),
      );
      setSelectedIds([]);
      queryClient.invalidateQueries({ queryKey: ["selection"] });
      queryClient.invalidateQueries({ queryKey: ["selection-image-ids"] });
    },
    onError: (e: any) => message.error("入仓失败：" + (e?.response?.data?.detail || e?.message)),
  });

  const items: any[] = data?.items || [];

  // 收集筛选选项
  const styleOptions = useMemo(() => {
    const set = new Set<string>();
    items.forEach((i) => { if (i.style_no) set.add(i.style_no); });
    return Array.from(set).sort().map((s) => ({ value: s, label: s }));
  }, [items]);

  const trendOptions = useMemo(() => {
    const set = new Set<string>();
    items.forEach((i) => { if (i.trend_name) set.add(i.trend_name); });
    return Array.from(set).sort().map((s) => ({ value: s, label: s }));
  }, [items]);

  // 应用筛选
  const filtered = useMemo(() => {
    return items.filter((i) => {
      if (filterStyle && i.style_no !== filterStyle) return false;
      if (filterTrend && i.trend_name !== filterTrend) return false;
      if (search) {
        const q = search.toLowerCase();
        const hay = `${i.task_id || ""} ${i.color_code || ""} ${i.color_name || ""} ${i.topic_id || ""} ${i.topic_name || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [items, filterStyle, filterTrend, search]);

  // 翻页：40 张/页，筛选变化回第 1 页
  const PAGE_SIZE = 40;
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [filterStyle, filterTrend, search]);
  const paged = useMemo(
    () => filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [filtered, page],
  );

  if (isLoading) return <Spin tip="加载所有生成图..." style={{ margin: 48 }} />;
  if (error) return <Empty description="加载失败" style={{ marginTop: 80 }} />;

  return (
    <div style={{ padding: 24, paddingBottom: composeMode ? 96 : 24 }}>
      <Space style={{ marginBottom: 16, width: "100%", justifyContent: "space-between" }}>
        <Space wrap>
          <h2 style={{ margin: 0 }}>全部生图</h2>
          <span style={{ fontSize: 13, color: "#999" }}>
            {filtered.length} / {items.length} 张
          </span>
        </Space>
        <Space>
          <span style={{ fontSize: 12, color: composeMode ? "#1677ff" : "#999", fontWeight: composeMode ? 600 : 400 }}>
            🛒 选款模式
          </span>
          <Switch
            checked={composeMode}
            onChange={(v) => {
              setComposeMode(v);
              if (!v) setSelectedIds([]);
              const next = new URLSearchParams(searchParams);
              if (v) next.set("compose", "1"); else next.delete("compose");
              setSearchParams(next, { replace: true });
            }}
          />
          <Link to="/selection"><Button>🛒 选款中心</Button></Link>
          <Link to="/fitting-room"><Button>👗 Fitting Room</Button></Link>
          <Link to="/tasks"><Button>← 任务列表</Button></Link>
        </Space>
      </Space>

      {/* 筛选条 */}
      <Card size="small" style={{ marginBottom: 12 }}>
        <Space wrap>
          <span style={{ fontSize: 12, color: "#666" }}>筛选：</span>
          <Select
            allowClear
            size="small"
            style={{ width: 200 }}
            placeholder="款号"
            value={filterStyle}
            onChange={setFilterStyle}
            options={styleOptions}
          />
          <Select
            allowClear
            size="small"
            style={{ width: 240 }}
            placeholder="趋势"
            value={filterTrend}
            onChange={setFilterTrend}
            options={trendOptions}
          />
          <Input
            allowClear
            size="small"
            style={{ width: 240 }}
            placeholder="搜色号 / 主题 / task ID"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {(filterStyle || filterTrend || search) && (
            <Button size="small" onClick={() => { setFilterStyle(null); setFilterTrend(null); setSearch(""); }}>
              清空筛选
            </Button>
          )}
        </Space>
      </Card>

      {filtered.length === 0 ? (
        <Empty description={items.length === 0 ? "还没有任何生成图" : "当前筛选下无结果"} style={{ marginTop: 60 }} />
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
            gap: 4,
          }}
        >
          {paged.map((it) => {
            const imageId = `${it.task_id}:${it.plan_id}`;
            const selIdx = selectedIds.indexOf(imageId);
            const cat = getCategoryForImage(it);
            return (
              <TileImage
                key={`${it.task_id}-${it.plan_id}`}
                item={it}
                composeMode={composeMode}
                selectionIndex={selIdx}
                category={cat}
                inStock={inStock.has(imageId)}
                onToggleCategory={() => toggleCategory(imageId, cat)}
                onClick={() => {
                  if (composeMode) toggleSelection(imageId);
                  else setModalItem(it);
                }}
                onPreview={(url) => setPreviewUrl(url)}
              />
            );
          })}
        </div>
      )}

      {/* 底部翻页器 */}
      {filtered.length > PAGE_SIZE && (
        <div style={{ display: "flex", justifyContent: "center", marginTop: 20 }}>
          <Pagination
            current={page}
            pageSize={PAGE_SIZE}
            total={filtered.length}
            showSizeChanger={false}
            showTotal={(t) => `共 ${t} 张`}
            onChange={(p) => { setPage(p); window.scrollTo({ top: 0 }); }}
          />
        </div>
      )}

      {/* 点图大图 Modal */}
      <Modal
        open={!!modalItem}
        onCancel={() => setModalItem(null)}
        width="80vw"
        footer={null}
        title={modalItem ? `${modalItem.color_code}-${modalItem.color_name} · ${modalItem.style_no}` : ""}
      >
        {modalItem && <ImageDetail item={modalItem} />}
      </Modal>

      {/* 全屏放大预览浮层（antd Image 内置：滚轮缩放 / 拖动 / 旋转 / 下载） */}
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

      {/* 选款模式：底部悬浮状态栏 */}
      {composeMode && (
        <div
          style={{
            position: "fixed",
            bottom: 0, left: 0, right: 0,
            background: "#fff",
            borderTop: "1px solid #d9d9d9",
            padding: "12px 24px",
            boxShadow: "0 -4px 12px rgba(0,0,0,0.08)",
            zIndex: 100,
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}
        >
          <Space size={16}>
            <span style={{ fontSize: 14 }}>
              已选 <strong style={{ color: "#1677ff" }}>{selectedIds.length}</strong> 张
            </span>
            <span style={{ fontSize: 11, color: "#999" }}>
              (点图多选，卡片右上角可切换 上装/下装 归类；入仓后去选款中心组套)
            </span>
          </Space>
          <Space>
            <Button
              onClick={() => setSelectedIds([])}
              disabled={selectedIds.length === 0}
            >
              清空选择
            </Button>
            <Button
              type="primary"
              onClick={() => addSelMut.mutate()}
              disabled={selectedIds.length === 0}
              loading={addSelMut.isPending}
            >
              🛒 加入选款中心 ({selectedIds.length})
            </Button>
          </Space>
        </div>
      )}
    </div>
  );
}


function TileImage({
  item, onClick, composeMode = false, selectionIndex = -1, category, onToggleCategory, onPreview,
  inStock = false,
}: {
  item: any;
  onClick: () => void;
  composeMode?: boolean;
  selectionIndex?: number;
  category?: "top" | "bottom";
  onToggleCategory?: () => void;
  onPreview?: (url: string) => void;
  inStock?: boolean;
}) {
  const isSelected = selectionIndex >= 0;
  return (
    <div
      onClick={onClick}
      style={{
        width: "100%",
        aspectRatio: "1/1",
        background: "#fafafa",
        overflow: "hidden",
        cursor: "pointer",
        position: "relative",
        borderRadius: 2,
        border: isSelected ? "3px solid #1677ff" : "1px solid #eee",
        boxShadow: isSelected ? "0 0 0 2px rgba(22,119,255,0.15)" : undefined,
        transition: "transform 0.15s, box-shadow 0.15s",
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLDivElement).style.transform = "scale(1.02)";
        if (!isSelected) (e.currentTarget as HTMLDivElement).style.boxShadow = "0 4px 12px rgba(0,0,0,0.12)";
        (e.currentTarget as HTMLDivElement).style.zIndex = "1";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLDivElement).style.transform = "";
        if (!isSelected) (e.currentTarget as HTMLDivElement).style.boxShadow = "";
        (e.currentTarget as HTMLDivElement).style.zIndex = "";
      }}
    >
      {/* 已入仓角标：右下角常显（选款状态一目了然） */}
      {inStock && (
        <div
          style={{
            position: "absolute",
            bottom: 6, right: 6,
            padding: "1px 7px",
            fontSize: 10, fontWeight: 600,
            borderRadius: 999,
            background: "rgba(82,196,26,0.92)",
            color: "#fff",
            zIndex: 3,
            pointerEvents: "none",
          }}
        >
          🛒 已入仓
        </div>
      )}
      {/* 选款模式：右上角 上装/下装 标签（可点切换） */}
      {composeMode && category && (
        <div
          onClick={(e) => { e.stopPropagation(); onToggleCategory?.(); }}
          title="点击切换上装 / 下装"
          style={{
            position: "absolute",
            top: 6, right: 6,
            padding: "2px 8px",
            fontSize: 10,
            fontWeight: 600,
            borderRadius: 999,
            background: category === "top" ? "rgba(22,119,255,0.9)" : "rgba(250,140,22,0.9)",
            color: "#fff",
            zIndex: 3,
            cursor: "pointer",
          }}
        >
          {category === "top" ? "上装" : "下装"}
        </div>
      )}
      {/* 组套模式：左上角选择编号浮层 */}
      {composeMode && isSelected && (
        <div
          style={{
            position: "absolute",
            top: 6, left: 6,
            width: 24, height: 24, borderRadius: 12,
            background: "#1677ff", color: "#fff",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 12, fontWeight: 700,
            zIndex: 3,
            boxShadow: "0 2px 4px rgba(0,0,0,0.3)",
          }}
        >
          {selectionIndex + 1}
        </div>
      )}
      {/* 🔍 放大预览 —— 组套已选中时让位给编号；其他时刻左上角常显 */}
      {!(composeMode && isSelected) && onPreview && (
        <div
          onClick={(e) => { e.stopPropagation(); onPreview(item.image_url); }}
          onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(0,0,0,0.85)"; }}
          onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(0,0,0,0.5)"; }}
          title="放大预览（滚轮缩放 / 拖动 / 旋转）"
          style={{
            position: "absolute", top: 6, left: 6,
            width: 24, height: 24, borderRadius: 4,
            background: "rgba(0,0,0,0.5)", color: "#fff",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 12,
            cursor: "zoom-in",
            transition: "background 0.15s",
            zIndex: 3,
          }}
        >🔍</div>
      )}
      <img
        src={item.image_url}
        alt={item.plan_id}
        style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
        loading="lazy"
      />
      {/* hover 显示色号 + 主题 + task */}
      <div
        className="gallery-overlay"
        style={{
          position: "absolute",
          bottom: 0, left: 0, right: 0,
          padding: "6px 8px",
          background: "linear-gradient(transparent, rgba(0,0,0,0.7))",
          color: "#fff",
          fontSize: 11,
          pointerEvents: "none",
          opacity: 0,
          transition: "opacity 0.15s",
        }}
      >
        <div style={{ fontWeight: 600 }}>
          {item.color_code} · {item.color_name}
        </div>
        <div style={{ fontSize: 10, opacity: 0.85 }}>
          {item.topic_id} {item.topic_name}
        </div>
        <div style={{ fontSize: 9, opacity: 0.7, marginTop: 2 }}>
          {item.style_no} · task {item.task_id?.slice(-8)}
        </div>
      </div>
      <style>{`
        div[style*="cursor: pointer"]:hover .gallery-overlay { opacity: 1 !important; }
      `}</style>
    </div>
  );
}


function ImageDetail({ item }: { item: any }) {
  const handleCopyPrompt = () => {
    if (item.image_prompt_used) {
      navigator.clipboard.writeText(item.image_prompt_used).then(
        () => message.success("已复制 prompt 到剪贴板"),
        () => message.error("复制失败"),
      );
    }
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <div>
        <Image
          src={item.image_url}
          alt={item.plan_id}
          style={{ width: "100%", borderRadius: 4 }}
          preview={{ mask: <span style={{ fontSize: 13 }}>🔍 点击放大</span> }}
        />
      </div>
      <div>
        <Space direction="vertical" style={{ width: "100%" }} size={4}>
          <div>
            <strong>方案编号：</strong>
            <code style={{ fontSize: 12 }}>{item.plan_id}</code>
          </div>
          <Space size={4} wrap>
            <Tag>{item.color_code}</Tag>
            <span>{item.color_name}</span>
            <Tag color="purple">{item.topic_id}</Tag>
            <span style={{ fontSize: 12 }}>{item.topic_name}</span>
          </Space>
          {item.方案说明 && (
            <div style={{ fontSize: 12 }}><strong>方案说明：</strong>{item.方案说明}</div>
          )}
          {typeof item.适配度 === "number" && (
            <div style={{ fontSize: 12 }}>
              <strong>适配度：</strong>
              <Tag color={item.适配度 >= 8 ? "green" : item.适配度 >= 6 ? "orange" : "red"}>
                {item.适配度}
              </Tag>
            </div>
          )}
          <div style={{ fontSize: 12, color: "#666" }}>
            <strong>来自任务：</strong>
            <Link to={`/tasks/${item.task_id}`}>
              <code>{item.task_id}</code>
            </Link>
          </div>
          <div style={{ fontSize: 12, color: "#999" }}>
            <strong>款号：</strong>{item.style_no} · <strong>趋势：</strong>{item.trend_name}
          </div>
          {item.elapsed_ms != null && (
            <div style={{ fontSize: 12, color: "#999" }}>
              生成耗时：{(item.elapsed_ms / 1000).toFixed(1)}s
            </div>
          )}
          {item.image_prompt_used && (
            <details>
              <summary style={{ cursor: "pointer", margin: "8px 0 4px", fontWeight: 600 }}>
                查看图生图 prompt
                <Button size="small" type="link" onClick={(e) => { e.preventDefault(); handleCopyPrompt(); }}>
                  复制
                </Button>
              </summary>
              <pre style={{
                background: "#1e1e1e", color: "#d4d4d4",
                padding: 12, fontSize: 11, whiteSpace: "pre-wrap",
                borderRadius: 4, maxHeight: 400, overflow: "auto",
              }}>
                {item.image_prompt_used}
              </pre>
            </details>
          )}
        </Space>
      </div>
    </div>
  );
}
