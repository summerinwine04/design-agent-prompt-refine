import {
  Alert, Button, Empty, Input, Modal, Pagination, Segmented, Select, Space, Spin, Tag, message,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  SelectionStyle, listSelection, removeSelection, uploadSelection,
  listAllImages, createLooksBulk, getSettings,
} from "../api/client";

/**
 * 🛒 选款中心 —— 已确认上架的 SKU 仓库
 *
 * 漏斗：生图任务（候选）→ 选款中心（确认上架）→ Fitting Room（组套）→ 波段（排期）
 *
 * - 展示槽位 高4:宽3，图片 contain 完整显示不裁切
 * - 上传图片入仓（类目+款号必填，不限比例）
 * - 组套模式：点一个上装 + 一个下装配一套，确认后批量建 look 进 Fitting Room
 * - 移除阻断：被 look 引用的款先拆引用
 * - 访客只读
 */
export default function SelectionCenterPage() {
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();

  // 访客探测（与 App 同 key 共享缓存）
  const { isError: isGuest } = useQuery({
    queryKey: ["access-probe"],
    queryFn: getSettings,
    retry: false,
    staleTime: Infinity,
  });

  const { data: items, isLoading } = useQuery({
    queryKey: ["selection"],
    queryFn: () => listSelection(),
  });

  // 生成图 URL 反查池
  const { data: allImages } = useQuery({
    queryKey: ["all-images"],
    queryFn: () => listAllImages(500),
  });
  const imageById = useMemo(() => {
    const m = new Map<string, any>();
    (allImages?.items || []).forEach((it: any) => m.set(`${it.task_id}:${it.plan_id}`, it));
    return m;
  }, [allImages]);

  // 筛选
  const [catFilter, setCatFilter] = useState<string>("all");     // all | top | bottom
  const [originFilter, setOriginFilter] = useState<string>("all");
  const [kw, setKw] = useState("");

  // 组套模式（?compose=1 直接进入）。
  // 点击顺序语义：
  //   上→下 = 一套完整 look；上→上 = 前一个封成「缺下装」look；
  //   下（无待配上装）= 自成「缺上装」look；下→下 = 两套「缺上装」look
  const [composeMode, setComposeMode] = useState(searchParams.get("compose") === "1");
  const [pendingTop, setPendingTop] = useState<SelectionStyle | null>(null);
  const [pairs, setPairs] = useState<Array<{ top?: SelectionStyle | null; bottom?: SelectionStyle | null }>>([]);

  const [uploadOpen, setUploadOpen] = useState(false);
  // 「设计灵感」详情弹窗：点击卡片打开（非组套模式）
  const [detailItem, setDetailItem] = useState<SelectionStyle | null>(null);

  // 用户视角的图片来源标签（内部 origin 是运营口径，对用户没意义）
  const sourceLabel = (it: SelectionStyle) =>
    it.source_kind === "uploaded" ? "用户自传" : "AI创作";

  const filtered = useMemo(() => {
    return (items || []).filter((it) => {
      if (catFilter !== "all" && it.category !== catFilter) return false;
      if (originFilter !== "all" && it.source_kind !== originFilter) return false;
      if (kw.trim()) {
        const hay = `${it.style_no || ""} ${it.color_code || ""} ${it.color_name || ""}`.toLowerCase();
        if (!hay.includes(kw.trim().toLowerCase())) return false;
      }
      return true;
    });
  }, [items, catFilter, originFilter, kw]);

  // 翻页：40 款/页，筛选变化回第 1 页
  const PAGE_SIZE = 40;
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [catFilter, originFilter, kw]);
  const paged = useMemo(
    () => filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [filtered, page],
  );

  const stats = useMemo(() => {
    const total = items?.length ?? 0;
    const top = (items || []).filter((it) => it.category === "top").length;
    const bottom = (items || []).filter((it) => it.category === "bottom").length;
    return { total, top, bottom };
  }, [items]);

  const removeMut = useMutation({
    mutationFn: (id: string) => removeSelection(id),
    onSuccess: () => {
      message.success("已移除出仓");
      queryClient.invalidateQueries({ queryKey: ["selection"] });
    },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail;
      if (e?.response?.status === 409 && detail?.looks) {
        Modal.warning({
          title: "移除被阻断",
          content: (
            <div>
              <p>{detail.message}</p>
              <ul style={{ marginTop: 8 }}>
                {detail.looks.map((lk: any) => (
                  <li key={lk.id}>
                    <Link to="/fitting-room" onClick={() => Modal.destroyAll()}>{lk.name}</Link>
                  </li>
                ))}
              </ul>
            </div>
          ),
        });
      } else {
        message.error("移除失败：" + (typeof detail === "string" ? detail : e?.message));
      }
    },
  });

  const composeSubmitMut = useMutation({
    mutationFn: (ps: Array<{ top?: SelectionStyle | null; bottom?: SelectionStyle | null }>) =>
      createLooksBulk(ps.map((p) => ({
        top_kind: p.top ? ("image" as const) : null,
        top_image_id: p.top?.image_id || null,
        bottom_kind: p.bottom ? ("image" as const) : null,
        bottom_image_id: p.bottom?.image_id || null,
      }))),
    onSuccess: (created) => {
      message.success(`已创建 ${created.length} 套 look，进入 Fitting Room 未分波段池（缺侧的可在看板补文本）`);
      setPairs([]); setPendingTop(null); setComposeMode(false);
      queryClient.invalidateQueries({ queryKey: ["looks"] });
      queryClient.invalidateQueries({ queryKey: ["waves"] });
    },
    onError: (e: any) => message.error("创建失败：" + (e?.response?.data?.detail || e?.message)),
  });

  // 组套点选（顺序语义，见上方注释）
  const handleComposePick = (it: SelectionStyle) => {
    if (!it.category) {
      message.warning("该款未标类目（上装/下装），无法参与组套");
      return;
    }
    if (it.category === "bottom") {
      if (pendingTop) {
        // 上→下：配成一套完整 look
        setPairs((prev) => [...prev, { top: pendingTop, bottom: it }]);
        setPendingTop(null);
      } else {
        // 无待配上装：下装自成一套「缺上装」look
        setPairs((prev) => [...prev, { bottom: it }]);
      }
    } else {
      if (pendingTop && pendingTop.id === it.id) {
        // 再点一次待配中的上装 = 取消待配
        setPendingTop(null);
        return;
      }
      if (pendingTop) {
        // 上→上：前一个封成「缺下装」look，新上装继续等下装
        setPairs((prev) => [...prev, { top: pendingTop }]);
      }
      setPendingTop(it);
    }
  };

  // 确认时把还在等下装的上装也封成「缺下装」look
  const finalLooks = useMemo(
    () => [...pairs, ...(pendingTop ? [{ top: pendingTop }] : [])],
    [pairs, pendingTop],
  );

  const imgUrlOf = (it: SelectionStyle): string | null => {
    if (it.source_kind === "uploaded") return it.upload_url;
    return imageById.get(it.image_id)?.image_url || null;
  };

  if (isLoading) return <Spin tip="加载仓库..." style={{ margin: 48 }} />;

  return (
    <div style={{ padding: 24 }}>
      {/* 顶部工具条 */}
      <Space style={{ marginBottom: 16, width: "100%", justifyContent: "space-between" }} wrap>
        <Space wrap>
          <h2 style={{ margin: 0 }}>🛒 选款中心</h2>
          <span style={{ fontSize: 13, color: "#999" }}>
            仓内 <strong>{stats.total}</strong> 款
            · 上装 <strong style={{ color: "#1677ff" }}>{stats.top}</strong>
            / 下装 <strong style={{ color: "#fa8c16" }}>{stats.bottom}</strong>
          </span>
        </Space>
        <Space wrap>
          {composeMode ? (
            <>
              <span style={{ fontSize: 13, color: "#1677ff" }}>
                👗 组套：
                {pendingTop
                  ? `上装 ${pendingTop.style_no || ""} 待配（点下装成套 / 再点它取消 / 点其他上装则它封为缺下装）`
                  : "点上装开始配对；直接点下装 = 单下装 look"}
                ，已配 <strong>{finalLooks.length}</strong> 套
              </span>
              <Button
                size="small"
                type="primary"
                disabled={finalLooks.length === 0}
                loading={composeSubmitMut.isPending}
                onClick={() => composeSubmitMut.mutate(finalLooks)}
              >
                ✅ 创建 {finalLooks.length} 套 look
              </Button>
              <Button size="small" onClick={() => {
                setComposeMode(false); setPairs([]); setPendingTop(null);
              }}>
                退出组套
              </Button>
            </>
          ) : (
            <>
              <Segmented
                value={catFilter}
                onChange={(v) => setCatFilter(String(v))}
                options={[
                  { label: "全部", value: "all" },
                  { label: "上装", value: "top" },
                  { label: "下装", value: "bottom" },
                ]}
              />
              <Select
                style={{ width: 120 }}
                value={originFilter}
                onChange={setOriginFilter}
                options={[
                  { value: "all", label: "全部来源" },
                  { value: "generated", label: "AI创作" },
                  { value: "uploaded", label: "用户自传" },
                ]}
              />
              <Input.Search
                placeholder="搜款号/色号/色名"
                style={{ width: 170 }}
                allowClear
                onSearch={setKw}
                onChange={(e) => { if (!e.target.value) setKw(""); }}
              />
              {!isGuest && (
                <>
                  <Button onClick={() => setUploadOpen(true)}>⬆ 上传款式</Button>
                  <Button
                    type="primary"
                    disabled={stats.total === 0}
                    onClick={() => setComposeMode(true)}
                  >
                    👗 组套模式
                  </Button>
                </>
              )}
            </>
          )}
        </Space>
      </Space>

      {/* 组套模式的已配对列表（缺侧的橙色提示） */}
      {composeMode && pairs.length > 0 && (
        <Space wrap style={{ marginBottom: 12 }}>
          {pairs.map((p, i) => (
            <Tag
              key={i}
              closable
              onClose={() => setPairs((prev) => prev.filter((_, j) => j !== i))}
              color={p.top && p.bottom ? "blue" : "orange"}
            >
              #{i + 1}{" "}
              {p.top
                ? `${p.top.style_no || "上装"}·${p.top.color_name || p.top.color_code || ""}`
                : "缺上装"}
              {" × "}
              {p.bottom
                ? `${p.bottom.style_no || "下装"}·${p.bottom.color_name || p.bottom.color_code || ""}`
                : "缺下装"}
            </Tag>
          ))}
        </Space>
      )}

      {stats.total === 0 ? (
        <Empty
          style={{ marginTop: 60 }}
          description={
            <span>
              仓库还是空的。去 <Link to="/tasks/gallery">全部生图</Link> 选款入仓，或点「⬆ 上传款式」。
            </span>
          }
        />
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(210px, 1fr))",
            gap: 14,
          }}
        >
          {paged.map((it) => {
            const url = imgUrlOf(it);
            const isPendingPick = !!pendingTop && pendingTop.id === it.id;
            const pairedCount = pairs.filter(
              (p) => p.top?.id === it.id || p.bottom?.id === it.id,
            ).length;
            return (
              <div
                key={it.id}
                onClick={composeMode ? () => handleComposePick(it) : () => setDetailItem(it)}
                style={{
                  border: isPendingPick ? "2px solid #1677ff" : "1px solid #eee",
                  borderRadius: 8,
                  overflow: "hidden",
                  cursor: "pointer",
                  background: "#fff",
                  boxShadow: isPendingPick ? "0 0 0 3px rgba(22,119,255,0.15)" : undefined,
                  position: "relative",
                }}
              >
                {/* 槽位：高4:宽3，contain 完整显示不裁切 */}
                <div style={{
                  width: "100%",
                  aspectRatio: "3 / 4",
                  background: "#f5f5f5",
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}>
                  {url ? (
                    <img
                      src={url}
                      alt={it.style_no || it.image_id}
                      loading="lazy"
                      style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block" }}
                    />
                  ) : (
                    <span style={{ fontSize: 11, color: "#bbb", textAlign: "center", padding: 8 }}>
                      图不可用<br />（源任务已删或未加载）
                    </span>
                  )}
                </div>
                {/* 角标 */}
                <div style={{ position: "absolute", top: 6, left: 6, display: "flex", gap: 4 }}>
                  {it.category && (
                    <Tag color={it.category === "top" ? "blue" : "orange"} style={{ margin: 0, fontSize: 10 }}>
                      {it.category === "top" ? "上装" : "下装"}
                    </Tag>
                  )}
                  {it.source_kind === "uploaded" && (
                    <Tag color="green" style={{ margin: 0, fontSize: 10 }}>上传</Tag>
                  )}
                </div>
                {pairedCount > 0 && (
                  <div style={{
                    position: "absolute", top: 6, right: 6,
                    background: "#1677ff", color: "#fff", borderRadius: 10,
                    fontSize: 11, fontWeight: 600, padding: "1px 8px",
                  }}>
                    已配 {pairedCount}
                  </div>
                )}
                {/* 信息条 */}
                <div style={{ padding: "8px 10px" }}>
                  <div style={{ fontSize: 12, fontWeight: 600 }}>
                    {it.style_no || (it.source_kind === "generated" ? imageById.get(it.image_id)?.style_no : "") || "—"}
                  </div>
                  <div style={{ fontSize: 11, color: "#888", marginTop: 2 }}>
                    {[it.color_code, it.color_name].filter(Boolean).join(" · ")
                      || (it.source_kind === "generated"
                          ? [imageById.get(it.image_id)?.color_code, imageById.get(it.image_id)?.color_name].filter(Boolean).join(" · ")
                          : "")
                      || " "}
                  </div>
                  <div style={{
                    marginTop: 6, display: "flex",
                    justifyContent: "space-between", alignItems: "center",
                  }}>
                    <Tag
                      color={it.source_kind === "uploaded" ? "green" : "geekblue"}
                      style={{ margin: 0, fontSize: 9 }}
                    >
                      {sourceLabel(it)}
                    </Tag>
                    {!isGuest && !composeMode && (
                      <Button
                        size="small"
                        danger
                        type="text"
                        style={{ fontSize: 11 }}
                        loading={removeMut.isPending && removeMut.variables === it.id}
                        onClick={(e) => { e.stopPropagation(); removeMut.mutate(it.id); }}
                      >
                        移除
                      </Button>
                    )}
                  </div>
                </div>
              </div>
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
            showTotal={(t) => `共 ${t} 款`}
            onChange={(p) => { setPage(p); window.scrollTo({ top: 0 }); }}
          />
        </div>
      )}

      {/* 设计灵感详情弹窗 */}
      <Modal
        open={!!detailItem}
        onCancel={() => setDetailItem(null)}
        width="76vw"
        footer={null}
        title={detailItem ? (
          detailItem.source_kind === "uploaded"
            ? `${detailItem.style_no || ""}${detailItem.color_name ? `-${detailItem.color_name}` : ""}（上传款）`
            : (() => {
                const g = imageById.get(detailItem.image_id) || {};
                return [g.style_no, g.color_code, g.color_name].filter(Boolean).join("-") || detailItem.image_id;
              })()
        ) : ""}
      >
        {detailItem && (
          <DesignInspiration
            item={detailItem}
            galleryItem={detailItem.source_kind === "generated" ? imageById.get(detailItem.image_id) : null}
            imgUrl={imgUrlOf(detailItem)}
          />
        )}
      </Modal>

      {/* 上传弹窗 */}
      <UploadModal
        open={uploadOpen}
        onCancel={() => setUploadOpen(false)}
        onDone={() => {
          setUploadOpen(false);
          queryClient.invalidateQueries({ queryKey: ["selection"] });
        }}
      />
    </div>
  );
}


// ========================================================================
// 设计灵感 —— 点击仓库卡片后的详情（主题名称 + 设计方案三合一）
// ========================================================================
function DesignInspiration({
  item, galleryItem, imgUrl,
}: {
  item: SelectionStyle;
  galleryItem: any | null;
  imgUrl: string | null;
}) {
  const g = galleryItem || {};
  const anchors: any[] = g["锚点列表"] || [];
  const hasPlanInfo = !!(g["图案内容"] || g["配色策略"] || anchors.length || g["方案说明"]);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
      <div style={{
        background: "#f5f5f5", borderRadius: 6,
        display: "flex", alignItems: "center", justifyContent: "center",
        minHeight: 320,
      }}>
        {imgUrl ? (
          <img src={imgUrl} alt="" style={{ maxWidth: "100%", maxHeight: "70vh", objectFit: "contain", display: "block" }} />
        ) : (
          <span style={{ color: "#bbb", fontSize: 12 }}>图不可用</span>
        )}
      </div>
      <div>
        <Space direction="vertical" style={{ width: "100%" }} size={8}>
          <Space wrap size={4}>
            {(item.color_code || g.color_code) && <Tag>{item.color_code || g.color_code}</Tag>}
            <span>{item.color_name || g.color_name}</span>
            {g["性别定向"] && <Tag>{g["性别定向"]}</Tag>}
            {item.category && (
              <Tag color={item.category === "top" ? "blue" : "orange"}>
                {item.category === "top" ? "上装" : "下装"}
              </Tag>
            )}
            <Tag color={item.source_kind === "uploaded" ? "green" : "geekblue"} style={{ margin: 0 }}>
              {item.source_kind === "uploaded" ? "用户自传" : "AI创作"}
            </Tag>
          </Space>

          <div style={{
            border: "1px solid #f0f0f0", borderRadius: 6, padding: "12px 14px",
            background: "#fafafa",
          }}>
            <div style={{ fontWeight: 700, marginBottom: 10, fontSize: 14 }}>💡 设计灵感</div>

            {/* 主题名称 = 印花图库的「主题+子方向」（文件夹名）/ 趋势报告的子主题名 */}
            <div style={{ marginBottom: 10 }}>
              <strong>主题名称：</strong>
              {g.topic_name ? (
                <>
                  {g.topic_id && <Tag color="purple" style={{ marginLeft: 4 }}>{g.topic_id}</Tag>}
                  <span>{g.topic_name}</span>
                </>
              ) : (
                <span style={{ color: "#999" }}>{item.source_kind === "uploaded" ? "上传款式，无主题信息" : "—"}</span>
              )}
            </div>

            {/* 设计方案 = 图案内容 + 配色策略 + 锚点 */}
            <div>
              <strong>设计方案：</strong>
              {hasPlanInfo ? (
                <div style={{ marginTop: 6, fontSize: 13, lineHeight: 1.8 }}>
                  {g["方案说明"] && (
                    <div style={{ color: "#666", marginBottom: 6 }}>{g["方案说明"]}</div>
                  )}
                  {g["图案内容"] && (
                    <div style={{ marginBottom: 6 }}>
                      <span style={{ color: "#999" }}>图案内容　</span>{g["图案内容"]}
                    </div>
                  )}
                  {g["配色策略"] && (
                    <div style={{ marginBottom: 6 }}>
                      <span style={{ color: "#999" }}>配色策略　</span>{g["配色策略"]}
                    </div>
                  )}
                  {anchors.length > 0 && (
                    <div>
                      <span style={{ color: "#999" }}>锚点</span>
                      {anchors.map((a: any, i: number) => (
                        <div key={i} style={{ fontSize: 12, marginLeft: 12 }}>
                          · {a.面} / {a.档} - {a.位置中文}{a.尺寸 ? ` (${a.尺寸})` : ""}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <span style={{ color: "#999", marginLeft: 4 }}>
                  {item.source_kind === "uploaded" ? "上传款式，无设计方案信息" : "该图的方案信息未加载（源任务可能超出加载范围）"}
                </span>
              )}
            </div>
          </div>

          {item.note && (
            <div style={{ fontSize: 12, color: "#666" }}><strong>备注：</strong>{item.note}</div>
          )}
          {g.task_id && (
            <div style={{ fontSize: 12, color: "#999" }}>
              来自任务 <Link to={`/tasks/${g.task_id}`}><code>{g.task_id}</code></Link>
              {g.elapsed_ms != null && <span> · 生成耗时 {(g.elapsed_ms / 1000).toFixed(1)}s</span>}
            </div>
          )}
          <div style={{ fontSize: 11, color: "#bbb" }}>入仓时间 {item.created_at}</div>
        </Space>
      </div>
    </div>
  );
}


// ========================================================================
// 上传款式弹窗：类目 + 款号必填；色名/备注选填；不限比例，≤20MB
// ========================================================================
function UploadModal({
  open, onCancel, onDone,
}: {
  open: boolean;
  onCancel: () => void;
  onDone: () => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [category, setCategory] = useState<"top" | "bottom" | null>(null);
  const [styleNo, setStyleNo] = useState("");
  const [colorName, setColorName] = useState("");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const reset = () => {
    setFile(null); setPreviewUrl(null); setCategory(null);
    setStyleNo(""); setColorName(""); setNote("");
  };

  const pickFile = (f: File) => {
    if (!/^image\//.test(f.type)) { message.error("请选择图片文件"); return; }
    if (f.size > 20 * 1024 * 1024) { message.error("图片超过 20MB 上限"); return; }
    setFile(f);
    setPreviewUrl(URL.createObjectURL(f));
  };

  const canSubmit = !!file && !!category && !!styleNo.trim();

  const submit = async () => {
    if (!canSubmit || !file || !category) return;
    setSubmitting(true);
    try {
      await uploadSelection(file, {
        category,
        style_no: styleNo.trim(),
        color_name: colorName.trim() || undefined,
        note: note.trim() || undefined,
      });
      message.success("已上传入仓");
      reset();
      onDone();
    } catch (e: any) {
      message.error("上传失败：" + (e?.response?.data?.detail || e?.message));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title="⬆ 上传款式入仓"
      onCancel={() => { reset(); onCancel(); }}
      okText="上传入仓"
      cancelText="取消"
      confirmLoading={submitting}
      okButtonProps={{ disabled: !canSubmit }}
      onOk={submit}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <input
          ref={fileRef}
          type="file"
          accept="image/jpeg,image/png,image/webp"
          style={{ display: "none" }}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) pickFile(f); e.target.value = ""; }}
        />
        <div
          onClick={() => fileRef.current?.click()}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer.files?.[0]; if (f) pickFile(f); }}
          style={{
            border: "1px dashed #d9d9d9", borderRadius: 6,
            padding: previewUrl ? 8 : 24, textAlign: "center",
            cursor: "pointer", background: "#fafafa",
          }}
        >
          {previewUrl ? (
            <img src={previewUrl} alt="预览" style={{ maxWidth: "100%", maxHeight: 240, objectFit: "contain" }} />
          ) : (
            <span style={{ fontSize: 12, color: "#999" }}>
              点击选择或拖入图片（jpg / png / webp，≤20MB，不限比例）
            </span>
          )}
        </div>
        <div>
          <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>类目 *</div>
          <Space size={8} align="center">
            {/* 三态区分：未选（橙色描边提示待选）→ 选中（蓝底白字，全局主题） */}
            <div style={{
              display: "inline-block",
              borderRadius: 8,
              border: category ? "1px solid transparent" : "1px solid #faad14",
              boxShadow: category ? undefined : "0 0 0 2px rgba(250,173,20,0.15)",
              transition: "border-color 0.15s, box-shadow 0.15s",
            }}>
              <Segmented
                value={category as any}
                onChange={(v) => setCategory(v as any)}
                options={[
                  { label: "👕 上装", value: "top" },
                  { label: "👖 下装", value: "bottom" },
                ]}
              />
            </div>
            {!category && (
              <span style={{ fontSize: 12, color: "#faad14" }}>← 请选择类目</span>
            )}
          </Space>
        </div>
        <div>
          <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>款号 *</div>
          <Input placeholder="如 YK250609（外部款可自由填写）" value={styleNo} onChange={(e) => setStyleNo(e.target.value)} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <div>
            <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>色名（选填）</div>
            <Input placeholder="如 浅天蓝" value={colorName} onChange={(e) => setColorName(e.target.value)} />
          </div>
          <div>
            <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>备注（选填）</div>
            <Input placeholder="来源/说明" value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
        </div>
        {!category || !styleNo.trim() ? (
          <Alert type="info" showIcon message="类目和款号必填——组套时按类目分槽位，导出时按款号命名" style={{ fontSize: 12 }} />
        ) : null}
      </Space>
    </Modal>
  );
}
