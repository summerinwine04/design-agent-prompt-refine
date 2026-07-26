import {
  Alert, Button, Card, Checkbox, Empty, Image, Input, Modal, Pagination, Space, Spin, Tag, Tooltip,
  Select, message, Segmented,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  Look, deleteLook, duplicateLook, exportLooksToDesktop,
  listAllImages, listLooks, updateLook,
  clearShootingSlot, getTemplatesData, setShootingSlotTemplate, uploadShootingSlot,
  TemplatesData,
  Wave, listWaves, createWave, updateWave, deleteWave,
  assignLooksToWave, unassignLooksFromWave, getSettings,
} from "../api/client";

/**
 * Fitting Room — 跨任务 look 组套管理看板
 *
 * 数据源：
 *   - GET /looks               所有 look 记录
 *   - GET /tasks/all-images    查图片 URL（look 里存的是 image_id = task_id:plan_id）
 *
 * 交互：
 *   - 顶部工具条：➕ 从 Gallery 组套（跳 /tasks/gallery?compose=1）+ 排序 + 导出 JSON + 统计
 *   - 主体：Look 看板 grid
 *   - 每卡：可编辑名字、上装图 + 下装图（或 textarea 补文本）、tags、复制 / 删除
 */
export default function FittingRoomPage() {
  const queryClient = useQueryClient();
  // 默认按创建（入库）时间倒序——顺序稳定，编辑不会导致卡片跳动；
  // 可切换「按更新时间」（最近编辑的在前）或「按名称」
  const [sortBy, setSortBy] = useState<"created" | "updated" | "name">("created");
  // 点缩略图放大预览
  const [previewItem, setPreviewItem] = useState<any>(null);
  // 打开模板库 picker 时记录当前 look（选定后要写回哪套）
  const [pickerTargetLookId, setPickerTargetLookId] = useState<string | null>(null);

  const { data: looks, isLoading: loadingLooks } = useQuery({
    queryKey: ["looks"],
    queryFn: () => listLooks(),
  });

  // 拉一次全图片池，建 image_id → item 映射
  const { data: allImages } = useQuery({
    queryKey: ["all-images"],
    queryFn: () => listAllImages(500),
  });

  const imageById = useMemo(() => {
    const m = new Map<string, any>();
    (allImages?.items || []).forEach((it: any) => {
      m.set(`${it.task_id}:${it.plan_id}`, it);
    });
    return m;
  }, [allImages]);

  const sortedLooks = useMemo(() => {
    const list = [...(looks || [])];
    if (sortBy === "name") {
      list.sort((a, b) => a.name.localeCompare(b.name));
    } else if (sortBy === "updated") {
      list.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    } else {
      // 默认：按创建（入库）时间倒序；同秒退化比 id（id 含毫秒时间戳）。
      // 编辑文本/tags 不会改变此顺序，卡片不跳位。
      list.sort((a, b) =>
        (b.created_at || "").localeCompare(a.created_at || "") || b.id.localeCompare(a.id),
      );
    }
    return list;
  }, [looks, sortBy]);

  const delMut = useMutation({
    mutationFn: (id: string) => deleteLook(id),
    onSuccess: () => {
      message.success("已删除");
      queryClient.invalidateQueries({ queryKey: ["looks"] });
    },
    onError: (e: any) => message.error("删除失败：" + (e?.message || String(e))),
  });

  const dupMut = useMutation({
    mutationFn: (id: string) => duplicateLook(id),
    onSuccess: () => {
      message.success("已复制");
      queryClient.invalidateQueries({ queryKey: ["looks"] });
    },
  });

  const updMut = useMutation({
    mutationFn: (payload: { id: string; changes: Partial<Look> }) =>
      updateLook(payload.id, payload.changes),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["looks"] }),
    onError: (e: any) => message.error("保存失败：" + (e?.message || String(e))),
  });

  // 多选态：进入后底部操作条同时提供 波段归组 + 导出（不预分目的，少一次选择）
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // ── 波段上新管理 ──────────────────────────────────────────
  const [viewMode, setViewMode] = useState<"flat" | "waves">("flat");
  const [waveFilter, setWaveFilter] = useState<string>("all");   // all | none | <wave_id>
  const [createWaveOpen, setCreateWaveOpen] = useState(false);
  const [collapsedWaves, setCollapsedWaves] = useState<Set<string>>(new Set());

  // 平铺视图翻页：20 组 look/页，筛选/排序/视图变化回第 1 页
  // （必须放在 waveFilter/viewMode 声明之后，否则 TDZ 报错白屏）
  const LOOK_PAGE_SIZE = 20;
  const [lookPage, setLookPage] = useState(1);
  useEffect(() => { setLookPage(1); }, [waveFilter, sortBy, viewMode]);
  const flatLooks = useMemo(
    () => sortedLooks.filter((lk) =>
      waveFilter === "all" ? true
      : waveFilter === "none" ? !lk.wave_id
      : lk.wave_id === waveFilter),
    [sortedLooks, waveFilter],
  );
  const pagedFlatLooks = useMemo(
    () => flatLooks.slice((lookPage - 1) * LOOK_PAGE_SIZE, lookPage * LOOK_PAGE_SIZE),
    [flatLooks, lookPage],
  );

  // 访客探测（与 App 同 queryKey 共享缓存，不发额外请求）：403 = 访客 → 波段只读
  const { isError: isGuest } = useQuery({
    queryKey: ["access-probe"],
    queryFn: getSettings,
    retry: false,
    staleTime: Infinity,
  });

  const { data: waves } = useQuery({ queryKey: ["waves"], queryFn: listWaves });
  const waveById = useMemo(
    () => new Map<string, Wave>((waves || []).map((w) => [w.id, w])),
    [waves],
  );

  const invalidateWaveData = () => {
    queryClient.invalidateQueries({ queryKey: ["waves"] });
    queryClient.invalidateQueries({ queryKey: ["looks"] });
  };
  const exitSelect = () => { setSelectMode(false); setSelectedIds(new Set()); };

  const waveCreateMut = useMutation({
    mutationFn: (p: { name: string; planned_launch_date?: string | null; look_ids?: string[] }) =>
      createWave(p),
    onSuccess: (w) => {
      message.success(`已创建波段「${w.name}」${w.look_count ? `，含 ${w.look_count} 套 look` : ""}`);
      setCreateWaveOpen(false);
      exitSelect();
      invalidateWaveData();
    },
    onError: (e: any) => message.error("创建波段失败：" + (e?.response?.data?.detail || e?.message)),
  });
  const waveUpdateMut = useMutation({
    mutationFn: (p: { id: string; changes: { name?: string; planned_launch_date?: string; status?: string } }) =>
      updateWave(p.id, p.changes),
    onSuccess: () => invalidateWaveData(),
    onError: (e: any) => message.error("更新波段失败：" + (e?.response?.data?.detail || e?.message)),
  });
  const waveDeleteMut = useMutation({
    mutationFn: (id: string) => deleteWave(id),
    onSuccess: (d: any) => {
      message.success(`波段已删除，${d?.released_looks ?? 0} 套 look 回到未分波段`);
      invalidateWaveData();
    },
    onError: (e: any) => message.error("删除波段失败：" + (e?.response?.data?.detail || e?.message)),
  });
  const assignMut = useMutation({
    mutationFn: (p: { waveId: string; lookIds: string[] }) => assignLooksToWave(p.waveId, p.lookIds),
    onSuccess: () => { message.success("已加入波段"); exitSelect(); invalidateWaveData(); },
    onError: (e: any) => message.error("归组失败：" + (e?.response?.data?.detail || e?.message)),
  });
  const unassignMut = useMutation({
    mutationFn: (lookIds: string[]) => unassignLooksFromWave(lookIds),
    onSuccess: () => { message.success("已移出波段"); exitSelect(); invalidateWaveData(); },
    onError: (e: any) => message.error("移出失败：" + (e?.response?.data?.detail || e?.message)),
  });

  const exportMut = useMutation({
    mutationFn: (ids: string[]) => exportLooksToDesktop(ids),
    onSuccess: (data) => {
      const okCount = data.exported_count;
      const skipCount = data.skipped.length;
      if (okCount > 0) {
        const csvTail = data.csv_path ? "，含 CSV manifest" : "";
        message.success(
          `已导出 ${okCount} 套到 ${data.output_dir}${csvTail}` +
            (skipCount > 0 ? `（${skipCount} 套跳过）` : ""),
          5,
        );
      } else {
        message.warning("没有 look 被成功导出，见详情", 4);
      }
      setSelectMode(false);
      setSelectedIds(new Set());
      queryClient.invalidateQueries({ queryKey: ["looks"] });
      if (skipCount > 0 || okCount === 0) {
        Modal.info({
          title: okCount > 0 ? `导出完成 — ${skipCount} 套/项跳过` : "导出失败",
          width: 680,
          content: (
            <div style={{ maxHeight: 400, overflow: "auto" }}>
              <p style={{ marginBottom: 6 }}>
                输出目录：<code style={{ background: "#f5f5f5", padding: "2px 6px" }}>{data.output_dir}</code>
              </p>
              {data.csv_path && (
                <p style={{ marginBottom: 12 }}>
                  CSV manifest：<code style={{ background: "#f5f5f5", padding: "2px 6px" }}>{data.csv_path}</code>
                </p>
              )}
              {data.exported.some((e) => e.partial_skips.length > 0) && (
                <>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>部分缺项的 look：</div>
                  <ul style={{ marginBottom: 12 }}>
                    {data.exported
                      .filter((e) => e.partial_skips.length > 0)
                      .map((e) => (
                        <li key={e.look_id}>
                          <strong>{e.look_name}</strong> — {e.partial_skips.join("; ")}
                        </li>
                      ))}
                  </ul>
                </>
              )}
              {data.skipped.length > 0 && (
                <>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>整套跳过的 look：</div>
                  <ul>
                    {data.skipped.map((s, i) => (
                      <li key={i}>
                        <strong>{s.look_name || s.look_id}</strong> — {s.reason}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          ),
        });
      }
    },
    onError: (e: any) => message.error("导出失败：" + (e?.message || String(e))),
  });

  const toggleSelectId = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (loadingLooks) return <Spin tip="加载 look..." style={{ margin: 48 }} />;

  const stats = (() => {
    const total = looks?.length ?? 0;
    const missing = (looks || []).filter(
      (lk) => !lk.top_image_id || !lk.bottom_image_id,
    ).length;
    const shootingConfigured = (looks || []).filter((lk) => !!lk.shooting_slot_url).length;
    return { total, missing, shootingConfigured };
  })();

  return (
    <div style={{ padding: 24 }}>
      {/* 顶部工具条：滚动时钉在导航栏下方（导航高 64px） */}
      <Space style={{
        marginBottom: 8, width: "100%", justifyContent: "space-between",
        position: "sticky", top: 64, zIndex: 150,
        background: "#f6f7f9", padding: "10px 0",
      }}>
        <Space wrap>
          <h2 style={{ margin: 0 }}>👗 Fitting Room</h2>
          <span style={{ fontSize: 13, color: "#999" }}>
            共 {stats.total} 套
            {stats.missing > 0 && (
              <> · <span style={{ color: "#faad14" }}>缺侧 {stats.missing} 套</span></>
            )}
            {stats.total > 0 && (
              <> · 拍摄参考 <strong style={{ color: stats.shootingConfigured === stats.total ? "#52c41a" : "#1677ff" }}>
                {stats.shootingConfigured}/{stats.total}
              </strong> 已配置</>
            )}
          </span>
        </Space>
        <Space wrap>
          {selectMode ? (
            <>
              <span style={{ fontSize: 13, color: "#1677ff" }}>
                已选 <strong>{selectedIds.size}</strong> / {stats.total} 套
              </span>
              <Button
                size="small"
                onClick={() => setSelectedIds(new Set((looks || []).map((lk) => lk.id)))}
              >
                全选
              </Button>
              <Button
                size="small"
                disabled={selectedIds.size === 0}
                onClick={() => setSelectedIds(new Set())}
              >
                清空
              </Button>
              {!isGuest && (
                <>
                  <span style={{ color: "#e8e8e8" }}>|</span>
                  <Select
                    size="small"
                    placeholder="🌊 加入波段..."
                    style={{ width: 150 }}
                    disabled={selectedIds.size === 0 || (waves || []).length === 0}
                    value={null as any}
                    options={(waves || []).map((w) => ({
                      value: w.id,
                      label: `${w.name}（${w.look_count}）`,
                    }))}
                    onSelect={(wid: any) =>
                      assignMut.mutate({ waveId: String(wid), lookIds: Array.from(selectedIds) })
                    }
                  />
                  <Button
                    size="small"
                    disabled={selectedIds.size === 0}
                    onClick={() => setCreateWaveOpen(true)}
                  >
                    ➕ 新波段
                  </Button>
                  <Button
                    size="small"
                    disabled={selectedIds.size === 0}
                    loading={unassignMut.isPending}
                    onClick={() => unassignMut.mutate(Array.from(selectedIds))}
                  >
                    移出波段
                  </Button>
                </>
              )}
              <span style={{ color: "#e8e8e8" }}>|</span>
              <Button
                size="small"
                type="primary"
                disabled={selectedIds.size === 0}
                loading={exportMut.isPending}
                onClick={() => exportMut.mutate(Array.from(selectedIds))}
              >
                📤 导出 {selectedIds.size} 套
              </Button>
              <Button size="small" onClick={exitSelect} disabled={exportMut.isPending}>
                取消
              </Button>
            </>
          ) : (
            <>
              <Link to="/selection?compose=1">
                <Button type="primary">➕ 去选款中心组套</Button>
              </Link>
              <Segmented
                value={viewMode}
                onChange={(v) => setViewMode(v as any)}
                options={[
                  { label: "平铺", value: "flat" },
                  { label: "🌊 波段", value: "waves" },
                ]}
              />
              {viewMode === "flat" && (
                <>
                  <Select
                    style={{ width: 116 }}
                    value={sortBy}
                    onChange={(v) => setSortBy(v as any)}
                    options={[
                      { value: "created", label: "按创建时间" },
                      { value: "updated", label: "按更新时间" },
                      { value: "name", label: "按名称" },
                    ]}
                  />
                  <Select
                    style={{ width: 130 }}
                    value={waveFilter}
                    onChange={setWaveFilter}
                    options={[
                      { value: "all", label: "全部波段" },
                      { value: "none", label: "未分波段" },
                      ...(waves || []).map((w) => ({ value: w.id, label: w.name })),
                    ]}
                  />
                </>
              )}
              <Button
                disabled={stats.total === 0}
                onClick={() => { setSelectMode(true); setSelectedIds(new Set()); }}
              >
                ☑ 多选（归组 / 导出）
              </Button>
            </>
          )}
        </Space>
      </Space>

      {stats.total === 0 ? (
        <Empty
          description={
            <span>
              还没有 look。先去 <Link to="/tasks/gallery?compose=1">全部生图</Link> 选款入仓，再到 <Link to="/selection?compose=1">选款中心 → 组套模式</Link> 配套。
            </span>
          }
          style={{ marginTop: 60 }}
        />
      ) : viewMode === "flat" ? (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(480px, 1fr))",
              gap: 16,
            }}
          >
            {pagedFlatLooks.map((lk) => (
              <LookCard
                key={lk.id}
                look={lk}
                waveName={lk.wave_id ? waveById.get(lk.wave_id)?.name : null}
                imageById={imageById}
                onDelete={() => delMut.mutate(lk.id)}
                onDuplicate={() => dupMut.mutate(lk.id)}
                onSave={(changes) => updMut.mutate({ id: lk.id, changes })}
                isSaving={updMut.isPending}
                onPreview={setPreviewItem}
                onOpenTemplatePicker={() => setPickerTargetLookId(lk.id)}
                selectMode={selectMode}
                selected={selectedIds.has(lk.id)}
                onToggleSelect={() => toggleSelectId(lk.id)}
              />
            ))}
          </div>
          {flatLooks.length > LOOK_PAGE_SIZE && (
            <div style={{ display: "flex", justifyContent: "center", marginTop: 20 }}>
              <Pagination
                current={lookPage}
                pageSize={LOOK_PAGE_SIZE}
                total={flatLooks.length}
                showSizeChanger={false}
                showTotal={(t) => `共 ${t} 组 look`}
                onChange={(p) => { setLookPage(p); window.scrollTo({ top: 0 }); }}
              />
            </div>
          )}
        </>
      ) : (
        // ── 波段聚合视图：未分波段置顶，其后按预备上架时间排序 ──
        <>
          {[
            { wave: null as Wave | null, looks: sortedLooks.filter((lk) => !lk.wave_id) },
            ...(waves || []).map((w) => ({
              wave: w as Wave | null,
              looks: sortedLooks.filter((lk) => lk.wave_id === w.id),
            })),
          ].map(({ wave, looks: sectionLooks }) => {
            const secKey = wave?.id || "__unassigned__";
            const collapsed = collapsedWaves.has(secKey);
            return (
              <div key={secKey} style={{ marginBottom: 20 }}>
                <WaveSectionHeader
                  wave={wave}
                  lookCount={wave ? wave.look_count : sectionLooks.length}
                  styleCount={wave ? wave.style_count : undefined}
                  collapsed={collapsed}
                  onToggleCollapse={() =>
                    setCollapsedWaves((prev) => {
                      const next = new Set(prev);
                      if (next.has(secKey)) next.delete(secKey); else next.add(secKey);
                      return next;
                    })
                  }
                  isGuest={!!isGuest}
                  onUpdate={(changes) => wave && waveUpdateMut.mutate({ id: wave.id, changes })}
                  onDelete={() => wave && Modal.confirm({
                    title: `删除波段「${wave.name}」？`,
                    icon: null,
                    content: `${wave.look_count} 套 look 将回到未分波段池（不会删除 look 本身）。`,
                    okText: "删除波段",
                    okType: "danger",
                    cancelText: "取消",
                    onOk: () => waveDeleteMut.mutate(wave.id),
                  })}
                  onExport={() => sectionLooks.length > 0 && exportMut.mutate(sectionLooks.map((lk) => lk.id))}
                  exporting={exportMut.isPending}
                />
                {!collapsed && (
                  sectionLooks.length === 0 ? (
                    <div style={{ padding: "12px 0 4px", fontSize: 12, color: "#bbb" }}>
                      （空波段——用「☑ 多选」勾选 look 后加进来）
                    </div>
                  ) : (
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "repeat(auto-fill, minmax(480px, 1fr))",
                        gap: 16,
                        marginTop: 12,
                      }}
                    >
                      {sectionLooks.map((lk) => (
                        <LookCard
                          key={lk.id}
                          look={lk}
                          imageById={imageById}
                          onDelete={() => delMut.mutate(lk.id)}
                          onDuplicate={() => dupMut.mutate(lk.id)}
                          onSave={(changes) => updMut.mutate({ id: lk.id, changes })}
                          isSaving={updMut.isPending}
                          onPreview={setPreviewItem}
                          onOpenTemplatePicker={() => setPickerTargetLookId(lk.id)}
                          selectMode={selectMode}
                          selected={selectedIds.has(lk.id)}
                          onToggleSelect={() => toggleSelectId(lk.id)}
                        />
                      ))}
                    </div>
                  )
                )}
              </div>
            );
          })}
        </>
      )}

      {/* 创建波段 Modal（多选归组时可顺带把已选 look 归入） */}
      <CreateWaveModal
        open={createWaveOpen}
        selectedCount={selectedIds.size}
        loading={waveCreateMut.isPending}
        onCancel={() => setCreateWaveOpen(false)}
        onSubmit={(name, date) =>
          waveCreateMut.mutate({
            name,
            planned_launch_date: date || null,
            look_ids: Array.from(selectedIds),
          })
        }
      />

      {/* 视觉模板库 Picker Modal */}
      <TemplatePickerModal
        open={!!pickerTargetLookId}
        onCancel={() => setPickerTargetLookId(null)}
        onPick={async (payload) => {
          if (!pickerTargetLookId) return;
          try {
            await setShootingSlotTemplate(pickerTargetLookId, payload);
            message.success("已配置拍摄参考");
            setPickerTargetLookId(null);
            queryClient.invalidateQueries({ queryKey: ["looks"] });
          } catch (e: any) {
            message.error("配置失败：" + (e?.message || String(e)));
          }
        }}
      />


      {/* 点缩略图放大 Modal */}
      <Modal
        open={!!previewItem}
        onCancel={() => setPreviewItem(null)}
        width="80vw"
        footer={null}
        title={previewItem ? `${previewItem.color_code}-${previewItem.color_name} · ${previewItem.style_no}` : ""}
      >
        {previewItem && <ImagePreview item={previewItem} />}
      </Modal>
    </div>
  );
}


// ========================================================================
// 波段区块头 —— 元信息条 + 行内编辑（wave=null 表示「未分波段」池）
// ========================================================================
function WaveSectionHeader({
  wave, lookCount, styleCount, collapsed, onToggleCollapse,
  isGuest, onUpdate, onDelete, onExport, exporting,
}: {
  wave: Wave | null;
  lookCount: number;
  styleCount?: number;
  collapsed: boolean;
  onToggleCollapse: () => void;
  isGuest: boolean;
  onUpdate: (changes: { name?: string; planned_launch_date?: string; status?: string }) => void;
  onDelete: () => void;
  onExport: () => void;
  exporting: boolean;
}) {
  const [editName, setEditName] = useState(false);
  const [nameDraft, setNameDraft] = useState(wave?.name || "");

  const commitName = () => {
    const v = nameDraft.trim();
    if (wave && v && v !== wave.name) onUpdate({ name: v });
    setEditName(false);
  };

  const fmt = (s?: string | null) => (s || "").slice(0, 16).replace("T", " ");

  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
        padding: "8px 12px",
        background: wave ? "#fafafa" : "#fffbe6",
        border: "1px solid #f0f0f0", borderRadius: 6,
      }}
    >
      <span
        onClick={onToggleCollapse}
        style={{ cursor: "pointer", fontSize: 12, color: "#999", userSelect: "none", width: 14 }}
        title={collapsed ? "展开" : "折叠"}
      >
        {collapsed ? "▶" : "▼"}
      </span>

      {wave ? (
        editName && !isGuest ? (
          <Input
            size="small"
            autoFocus
            value={nameDraft}
            onChange={(e) => setNameDraft(e.target.value)}
            onPressEnter={commitName}
            onBlur={commitName}
            style={{ width: 180 }}
          />
        ) : (
          <strong
            style={{ fontSize: 14, cursor: isGuest ? "default" : "pointer" }}
            title={isGuest ? "" : "点击重命名"}
            onClick={() => { if (!isGuest) { setNameDraft(wave.name); setEditName(true); } }}
          >
            🌊 {wave.name}
          </strong>
        )
      ) : (
        <strong style={{ fontSize: 14 }}>📥 未分波段</strong>
      )}

      {wave && (
        <Tag
          color={wave.status === "已上架" ? "green" : "blue"}
          style={{ margin: 0, cursor: isGuest ? "default" : "pointer" }}
          title={isGuest ? "" : "点击切换状态"}
          onClick={() => {
            if (isGuest) return;
            onUpdate({ status: wave.status === "已上架" ? "规划中" : "已上架" });
          }}
        >
          {wave.status}
        </Tag>
      )}

      {wave && (
        <span style={{ fontSize: 12, color: "#666", display: "inline-flex", alignItems: "center", gap: 4 }}>
          预备上架
          {isGuest ? (
            <strong>{wave.planned_launch_date || "未定"}</strong>
          ) : (
            <Input
              type="date"
              size="small"
              value={wave.planned_launch_date || ""}
              onChange={(e) => onUpdate({ planned_launch_date: e.target.value })}
              style={{ width: 130, fontSize: 12 }}
            />
          )}
        </span>
      )}

      <span style={{ fontSize: 12, color: "#666" }}>
        <strong style={{ color: "#722ed1" }}>{styleCount ?? "-"}</strong> 款
        · <strong style={{ color: "#1677ff" }}>{lookCount}</strong> look
      </span>

      {wave && (
        <span style={{ fontSize: 11, color: "#bbb" }}>
          创建 {fmt(wave.created_at)} · 更新 {fmt(wave.updated_at)}
        </span>
      )}

      <span style={{ flex: 1 }} />

      {wave && (
        <Space size={4}>
          <Button size="small" loading={exporting} disabled={lookCount === 0} onClick={onExport}>
            📤 导出本波段
          </Button>
          {!isGuest && (
            <Button size="small" danger onClick={onDelete}>删除波段</Button>
          )}
        </Space>
      )}
    </div>
  );
}


// ========================================================================
// 创建波段 Modal
// ========================================================================
function CreateWaveModal({
  open, selectedCount, loading, onCancel, onSubmit,
}: {
  open: boolean;
  selectedCount: number;
  loading: boolean;
  onCancel: () => void;
  onSubmit: (name: string, date: string) => void;
}) {
  const [name, setName] = useState("");
  const [date, setDate] = useState("");

  return (
    <Modal
      open={open}
      title="➕ 创建新波段"
      onCancel={onCancel}
      okText="创建"
      cancelText="取消"
      confirmLoading={loading}
      okButtonProps={{ disabled: !name.trim() }}
      onOk={() => { onSubmit(name.trim(), date); setName(""); setDate(""); }}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        {selectedCount > 0 && (
          <Alert
            type="info"
            showIcon
            message={`已选的 ${selectedCount} 套 look 将直接归入新波段`}
          />
        )}
        <div>
          <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>波段名 *</div>
          <Input
            autoFocus
            placeholder="如：8月第1波 / 秋季开学波"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onPressEnter={() => name.trim() && onSubmit(name.trim(), date)}
          />
        </div>
        <div>
          <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>预备上架时间（可选）</div>
          <Input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            style={{ width: 180 }}
          />
        </div>
      </Space>
    </Modal>
  );
}


function ImagePreview({ item }: { item: any }) {
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
        <img src={item.image_url} alt={item.plan_id} style={{ width: "100%", borderRadius: 4 }} />
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
            {item.topic_id && <Tag color="purple">{item.topic_id}</Tag>}
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


function LookCard({
  look, imageById, onDelete, onDuplicate, onSave, isSaving, onPreview, onOpenTemplatePicker,
  selectMode = false, selected = false, onToggleSelect, waveName = null,
}: {
  look: Look;
  imageById: Map<string, any>;
  waveName?: string | null;
  onDelete: () => void;
  onDuplicate: () => void;
  onSave: (changes: Partial<Look>) => void;
  isSaving: boolean;
  onPreview: (item: any) => void;
  onOpenTemplatePicker: () => void;
  selectMode?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}) {
  const [editName, setEditName] = useState(false);
  const [nameDraft, setNameDraft] = useState(look.name);

  const topImage = look.top_image_id ? imageById.get(look.top_image_id) : null;
  const bottomImage = look.bottom_image_id ? imageById.get(look.bottom_image_id) : null;
  const isMissingTop = !look.top_image_id;
  const isMissingBottom = !look.bottom_image_id;

  const handleNameCommit = () => {
    const v = nameDraft.trim();
    if (v && v !== look.name) onSave({ name: v });
    setEditName(false);
  };

  return (
    <Card
      size="small"
      bodyStyle={{ padding: 12 }}
      style={{
        cursor: selectMode ? "pointer" : undefined,
        borderColor: selectMode && selected ? "#1677ff" : undefined,
        borderWidth: selectMode && selected ? 2 : 1,
        boxShadow: selectMode && selected ? "0 0 0 2px rgba(22,119,255,0.12)" : undefined,
        transition: "border-color 0.15s, box-shadow 0.15s",
      }}
      onClick={selectMode ? (e) => {
        // 卡片点选：避免命中 checkbox / 内部按钮时冒泡到这里再切一次
        const t = e.target as HTMLElement;
        if (t.closest("input, button, .ant-btn, .ant-checkbox, .ant-image, textarea, a")) return;
        onToggleSelect?.();
      } : undefined}
      title={
        <Space style={{ width: "100%", justifyContent: "space-between" }} size={4}>
          <Space size={6} align="center">
            {selectMode && (
              <Checkbox
                checked={selected}
                onClick={(e) => e.stopPropagation()}
                onChange={() => onToggleSelect?.()}
              />
            )}
            {editName ? (
              <Input
                size="small"
                autoFocus
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                onPressEnter={handleNameCommit}
                onBlur={handleNameCommit}
                style={{ width: 200 }}
              />
            ) : (
              <span
                style={{ cursor: selectMode ? "pointer" : "pointer", fontWeight: 600 }}
                onClick={(e) => {
                  if (selectMode) return;   // select mode 下 name 不允许编辑，避免误触
                  e.stopPropagation();
                  setNameDraft(look.name);
                  setEditName(true);
                }}
                title={selectMode ? "" : "点击重命名"}
              >
                {look.name}
              </span>
            )}
          </Space>
          <Space size={4}>
            <Tooltip title="复制此 look">
              <Button size="small" onClick={onDuplicate}>⎘</Button>
            </Tooltip>
            <Tooltip title="删除">
              <Button size="small" danger onClick={() =>
                Modal.confirm({
                  title: `删除 look 「${look.name}」？`,
                  icon: null,
                  content: "只删记录，不影响关联的生图任务本身。",
                  okText: "删除",
                  okType: "danger",
                  cancelText: "取消",
                  onOk: onDelete,
                })
              }>×</Button>
            </Tooltip>
          </Space>
        </Space>
      }
    >
      {/* 上装 | 下装 | 拍摄参考 三列，槽位尺寸一致（3:4 contain） */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 8 }}>
        {/* 上装侧 —— 有图时只存文本（kind 保持 image）；缺图时文本兼作该侧内容（kind=text） */}
        <SlotView
          side="top"
          image={topImage}
          text={look.top_text}
          isMissing={isMissingTop}
          onSaveText={(t) => onSave(topImage ? { top_text: t } : { top_text: t, top_kind: t ? "text" : null })}
          isSaving={isSaving}
          onImageClick={() => topImage && onPreview(topImage)}
        />
        {/* 下装侧 */}
        <SlotView
          side="bottom"
          image={bottomImage}
          text={look.bottom_text}
          isMissing={isMissingBottom}
          onSaveText={(t) => onSave(bottomImage ? { bottom_text: t } : { bottom_text: t, bottom_kind: t ? "text" : null })}
          isSaving={isSaving}
          onImageClick={() => bottomImage && onPreview(bottomImage)}
        />
        {/* 拍摄参考：第三列，槽位与服装图一致 */}
        <ShootingSlotView
          lookId={look.id}
          slotKind={look.shooting_slot_kind}
          slotUrl={look.shooting_slot_url}
          slotMeta={look.shooting_slot_meta}
          onOpenTemplatePicker={onOpenTemplatePicker}
          onPreview={onPreview}
        />
      </div>

      {/* 元信息 */}
      <div style={{ marginTop: 6, fontSize: 10, color: "#bbb", display: "flex", alignItems: "center", gap: 6 }}>
        {waveName && (
          <Tag color="purple" style={{ margin: 0, fontSize: 9, lineHeight: "16px" }}>
            🌊 {waveName}
          </Tag>
        )}
        <span>更新 {look.updated_at}</span>
      </div>
      {/* 导出记录：新格式 "已导出:YYYY-MM-DD HH:MM" → "MM-DD HH:MM 导出"；旧格式兜底 "已导出" */}
      {(() => {
        const t = (look.tags || []).find((x) => String(x).startsWith("已导出"));
        if (!t) return null;
        const ts = String(t).includes(":") ? String(t).slice(String(t).indexOf(":") + 1) : "";
        return (
          <div style={{ marginTop: 2, fontSize: 10, color: "#bbb" }}>
            {ts ? `${ts.slice(5)} 导出` : "已导出"}
          </div>
        );
      })()}
    </Card>
  );
}


function SlotView({
  side, image, text, isMissing, onSaveText, isSaving, onImageClick,
}: {
  side: "top" | "bottom";
  image: any | null;
  text: string | null | undefined;
  isMissing: boolean;
  onSaveText: (text: string) => void;
  isSaving: boolean;
  onImageClick?: () => void;
}) {
  const label = side === "top" ? "上装" : "下装";
  const [textDraft, setTextDraft] = useState(text || "");

  // 常驻文本录入框：有图无图都显示，onBlur 保存，随导出写入 CSV 的「{label}文本表达」列
  const textInput = (
    <Input.TextArea
      rows={2}
      placeholder={`${label}文本表达（随导出写入 CSV）...`}
      value={textDraft}
      onChange={(e) => setTextDraft(e.target.value)}
      onBlur={() => {
        if (textDraft.trim() !== (text || "").trim()) onSaveText(textDraft.trim());
      }}
      disabled={isSaving}
      style={{ fontSize: 11, marginTop: 4 }}
    />
  );

  // 有图 → 显示图 + 下方文本框
  if (image?.image_url) {
    return (
      <div>
        <div style={{ fontSize: 11, color: "#666", marginBottom: 4 }}>
          <Tag color={side === "top" ? "blue" : "orange"} style={{ margin: 0, fontSize: 10 }}>
            {label}
          </Tag>
        </div>
        <div
          onClick={onImageClick}
          title="点击放大查看原图"
          style={{
            position: "relative",
            cursor: onImageClick ? "zoom-in" : "default",
            transition: "transform 0.15s",
          }}
          onMouseEnter={(e) => { if (onImageClick) (e.currentTarget as HTMLDivElement).style.transform = "scale(1.02)"; }}
          onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.transform = ""; }}
        >
          {/* 槽位与选款中心一致：高4:宽3，contain 完整显示不裁切 */}
          <div style={{
            width: "100%",
            aspectRatio: "3/4",
            background: "#f5f5f5",
            borderRadius: 4,
            border: "1px solid #eee",
            display: "flex", alignItems: "center", justifyContent: "center",
            overflow: "hidden",
          }}>
            <img
              src={image.image_url}
              alt={label}
              style={{
                maxWidth: "100%",
                maxHeight: "100%",
                objectFit: "contain",
                display: "block",
              }}
            />
          </div>
          {onImageClick && (
            <div style={{
              position: "absolute",
              top: 4, right: 4,
              width: 22, height: 22,
              background: "rgba(0,0,0,0.5)",
              color: "#fff",
              borderRadius: 4,
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 12,
              pointerEvents: "none",
            }}>🔍</div>
          )}
        </div>
        <div style={{ fontSize: 10, color: "#888", marginTop: 4 }}>
          {image.color_code} · {image.color_name}
        </div>
        {image.topic_name && (
          <div style={{ fontSize: 10, color: "#999" }}>{image.topic_id} {image.topic_name}</div>
        )}
        {textInput}
      </div>
    );
  }

  // 引用了 image_id 但拉不到图（task 已删 / URL 失效）
  if (!isMissing) {
    return (
      <div>
        <div style={{ fontSize: 11, color: "#666", marginBottom: 4 }}>
          <Tag color={side === "top" ? "blue" : "orange"} style={{ margin: 0, fontSize: 10 }}>{label}</Tag>
        </div>
        <div
          style={{
            width: "100%", aspectRatio: "3/4", background: "#fafafa",
            border: "1px dashed #ddd", borderRadius: 4,
            display: "flex", alignItems: "center", justifyContent: "center",
            color: "#bbb", fontSize: 11, textAlign: "center", padding: 4,
          }}
        >
          图不可用<br />（source 已失效）
        </div>
        {textInput}
      </div>
    );
  }

  // 缺该侧款图 → 文本框即该侧内容（kind=text），行数多一点
  return (
    <div>
      <div style={{ fontSize: 11, color: "#faad14", marginBottom: 4 }}>
        ⚠ 缺{label}
      </div>
      <Input.TextArea
        rows={4}
        placeholder={`描述这套 look 的${label}期望...`}
        value={textDraft}
        onChange={(e) => setTextDraft(e.target.value)}
        onBlur={() => {
          if (textDraft.trim() !== (text || "").trim()) onSaveText(textDraft.trim());
        }}
        disabled={isSaving}
        style={{ fontSize: 11 }}
      />
    </div>
  );
}


// ========================================================================
// 拍摄参考槽位 — 空态三入口 / 已配置态
// ========================================================================
function ShootingSlotView({
  lookId, slotKind, slotUrl, slotMeta, onOpenTemplatePicker, onPreview,
}: {
  lookId: string;
  slotKind: "upload" | "template" | null;
  slotUrl: string | null;
  slotMeta: Record<string, any> | null;
  onOpenTemplatePicker: () => void;
  onPreview: (item: any) => void;
}) {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);

  const handleUpload = async (file: File) => {
    setIsUploading(true);
    try {
      await uploadShootingSlot(lookId, file);
      message.success("已上传作为拍摄参考");
      queryClient.invalidateQueries({ queryKey: ["looks"] });
    } catch (e: any) {
      message.error("上传失败：" + (e?.response?.data?.detail || e?.message || String(e)));
    } finally {
      setIsUploading(false);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) handleUpload(f);
    e.target.value = "";
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const f = e.dataTransfer.files?.[0];
    if (f && /^image\//.test(f.type)) handleUpload(f);
    else if (f) message.error("请拖入图片文件");
  };

  const handleClear = async () => {
    try {
      await clearShootingSlot(lookId);
      message.success("已清空拍摄参考");
      queryClient.invalidateQueries({ queryKey: ["looks"] });
    } catch (e: any) {
      message.error("清空失败：" + (e?.message || String(e)));
    }
  };

  // 组装 tag 显示（截取 4 个最有用的）
  const shortTags = (() => {
    if (!slotMeta) return [];
    const t: string[] = [];
    if (slotMeta.category) t.push(slotMeta.category);
    // group_tags 通常是 {label: [values]} 形式，取第一个 label 的第一个 value
    const gt = slotMeta.group_tags || {};
    for (const [_k, vs] of Object.entries(gt)) {
      if (Array.isArray(vs) && vs.length) { t.push(String(vs[0])); if (t.length >= 3) break; }
      else if (typeof vs === "string") { t.push(vs); if (t.length >= 3) break; }
    }
    // image_tags 通常是 {label: value} 形式
    const it = slotMeta.image_tags || {};
    for (const [_k, v] of Object.entries(it)) {
      if (v) { t.push(String(v)); if (t.length >= 5) break; }
    }
    return t;
  })();

  return (
    <div>
      {/* 与 上装/下装 列头保持同款式 */}
      <div style={{ fontSize: 11, color: "#666", marginBottom: 4 }}>
        <Tag color="purple" style={{ margin: 0, fontSize: 10 }}>📸 拍摄参考</Tag>
      </div>

      {/* 隐藏 file input 供空态和"换一张"复用 */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        style={{ display: "none" }}
        onChange={handleFileChange}
      />

      {slotUrl ? (
        // 已配置态：槽位与服装图一致（3:4 contain），操作按钮放槽位下方
        <>
          <div
            title="点击放大"
            onClick={() => onPreview({
              image_url: slotUrl,
              plan_id: "shooting-slot",
              color_code: slotKind === "template" ? "模板" : "上传",
              color_name: slotMeta?.image_filename || slotMeta?.original_filename || "参考图",
              style_no: slotMeta?.group_name || "",
              topic_name: shortTags.join(" · "),
              image_prompt_used: null,
            })}
            style={{
              width: "100%",
              aspectRatio: "3/4",
              background: "#f5f5f5",
              borderRadius: 4,
              border: "1px solid #eee",
              display: "flex", alignItems: "center", justifyContent: "center",
              overflow: "hidden",
              cursor: "zoom-in",
            }}
          >
            <img
              src={slotUrl}
              alt="拍摄参考"
              style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block" }}
              onError={(e) => { (e.target as HTMLImageElement).style.opacity = "0.3"; }}
            />
          </div>
          <Space size={2} wrap style={{ marginTop: 4 }}>
            <Button size="small" style={{ fontSize: 11, padding: "0 6px" }} onClick={() => fileInputRef.current?.click()}>🔄 上传</Button>
            <Button size="small" style={{ fontSize: 11, padding: "0 6px" }} onClick={onOpenTemplatePicker}>📂 模板</Button>
            <Button size="small" danger style={{ fontSize: 11, padding: "0 6px" }} onClick={handleClear}>×</Button>
          </Space>
        </>
      ) : (
        // 空态 — 拖拽区（槽位同尺寸）+ 两入口
        <div
          onDragEnter={(e) => { e.preventDefault(); setIsDragging(true); }}
          onDragLeave={(e) => { e.preventDefault(); setIsDragging(false); }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={handleDrop}
          style={{
            border: `1px dashed ${isDragging ? "#1677ff" : "#d9d9d9"}`,
            background: isDragging ? "#e6f4ff" : "#fafafa",
            borderRadius: 4,
            width: "100%",
            aspectRatio: "3/4",
            display: "flex", alignItems: "center", justifyContent: "center",
            padding: 8,
            textAlign: "center",
            transition: "all 0.15s",
          }}
        >
          {isUploading ? (
            <Space direction="vertical" size={4}>
              <Spin size="small" />
              <span style={{ fontSize: 11, color: "#999" }}>上传中...</span>
            </Space>
          ) : (
            <Space direction="vertical" size={6}>
              <span style={{ fontSize: 11, color: "#999" }}>
                拖入图片，或
              </span>
              <Space size={4}>
                <Button size="small" onClick={() => fileInputRef.current?.click()}>↑ 上传</Button>
                <Button size="small" type="primary" onClick={onOpenTemplatePicker}>📂 模板库</Button>
              </Space>
              <span style={{ fontSize: 10, color: "#bbb" }}>
                jpg / png / webp · 最大 20MB
              </span>
            </Space>
          )}
        </div>
      )}
    </div>
  );
}


// ========================================================================
// 视觉模板库 Picker Modal
// ========================================================================
function TemplatePickerModal({
  open, onCancel, onPick,
}: {
  open: boolean;
  onCancel: () => void;
  onPick: (payload: {
    category: string;
    group_name: string;
    image_filename: string;
    group_tags?: Record<string, any>;
    image_tags?: Record<string, any>;
  }) => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["templates-data"],
    queryFn: () => getTemplatesData(),
    enabled: open,
  });

  const [selectedCat, setSelectedCat] = useState<string>("全部");
  const [picked, setPicked] = useState<{
    category: string; group_name: string; image_filename: string;
    group_tags?: any; image_tags?: any;
  } | null>(null);
  // 缩略图放大预览 URL——独立于 picked 状态，让"预览"和"选中"互不干扰
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  const categories = data?.meta?.categories || [];
  const cats = ["全部", ...categories];

  const filteredGroups = useMemo(() => {
    if (!data?.groups) return [];
    if (selectedCat === "全部") return data.groups;
    return data.groups.filter((g) => g.category === selectedCat);
  }, [data, selectedCat]);

  const handleSubmit = () => {
    if (!picked) return;
    onPick(picked);
    setPicked(null);
  };

  return (
    <Modal
      open={open}
      onCancel={() => { setPicked(null); onCancel(); }}
      title={
        <Space>
          <span>📂 视觉模板库</span>
          {data && (
            <span style={{ fontSize: 12, color: "#999" }}>
              {data.groups?.length ?? 0} 组 · 共 {data.meta?.image_count ?? 0} 张
            </span>
          )}
        </Space>
      }
      width="90vw"
      style={{ top: 24 }}
      footer={
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <span style={{ fontSize: 12, color: "#999" }}>
            {picked ? `已选：${picked.category} · ${picked.group_name} · ${picked.image_filename}` : "点选一张图作为拍摄参考"}
          </span>
          <Space>
            <Button onClick={() => { setPicked(null); onCancel(); }}>取消</Button>
            <Button type="primary" disabled={!picked} onClick={handleSubmit}>
              选定并返回
            </Button>
          </Space>
        </Space>
      }
    >
      {isLoading ? (
        <Spin tip="加载模板库..." style={{ margin: 48 }} />
      ) : error || !data ? (
        <Empty description="加载模板库失败" />
      ) : data.error ? (
        <Alert
          type="warning"
          message="模板库不可用"
          description={
            <div style={{ fontSize: 12 }}>
              <div>{data.error}</div>
              <div style={{ marginTop: 8, color: "#999" }}>
                在 <code>.env</code> 里配置 <code>TEMPLATES_ROOT=&lt;绝对路径&gt;</code> 指向视觉模板库文件夹，重启 backend 后重试。
              </div>
            </div>
          }
        />
      ) : (
        <>
          {/* 类目切换 —— 与全平台 Segmented 统一（选中蓝底白字） */}
          <div style={{ marginBottom: 16 }}>
            <Segmented
              value={selectedCat}
              onChange={(v) => setSelectedCat(String(v))}
              options={cats.map((c) => ({
                value: c,
                label: `${c} ${c === "全部" ? (data.groups?.length ?? 0) : (data.groups?.filter((g) => g.category === c).length ?? 0)}`,
              }))}
            />
          </div>

          {/* 图组列表 */}
          <div style={{ maxHeight: "70vh", overflowY: "auto" }}>
            {filteredGroups.length === 0 ? (
              <Empty description="该类目下无图组" />
            ) : (
              filteredGroups.map((g) => (
                <TemplateGroupBlock
                  key={`${g.category}-${g.name}`}
                  group={g}
                  pickedFilename={picked?.category === g.category && picked?.group_name === g.name ? picked.image_filename : null}
                  onPickImage={(image) => setPicked({
                    category: g.category,
                    group_name: g.name,
                    image_filename: image.filename,
                    group_tags: g.group_tags || {},
                    image_tags: image.tags || {},
                  })}
                  onPreview={(url) => setPreviewUrl(url)}
                />
              ))
            )}
          </div>
        </>
      )}

      {/* 隐藏渲染 antd Image，只用它的 preview 层显示大图 */}
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
    </Modal>
  );
}


function TemplateGroupBlock({
  group, pickedFilename, onPickImage, onPreview,
}: {
  group: TemplatesData["groups"][0];
  pickedFilename: string | null;
  onPickImage: (image: TemplatesData["groups"][0]["images"][0]) => void;
  onPreview: (url: string) => void;
}) {
  return (
    <div style={{ marginBottom: 20, borderBottom: "1px solid #f0f0f0", paddingBottom: 16 }}>
      <Space style={{ marginBottom: 8 }}>
        <Tag color="purple">{group.category}</Tag>
        <strong>{group.name}</strong>
        {group.model_name && (
          <span style={{ fontSize: 12, color: "#666" }}>· 模特 {group.model_name}</span>
        )}
        {group.scene_desc && (
          <span style={{ fontSize: 12, color: "#666" }}>· {group.scene_desc}</span>
        )}
      </Space>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))",
          gap: 6,
        }}
      >
        {group.images.map((img) => {
          // JSON 里字段叫 file（完整相对路径 templates/类目/图组/xxx.jpg），兼容旧 filename
          const relPath = img.file || img.filename || "";
          const basename = relPath.split("/").pop() || relPath;
          // 每一段单独编码后再 join，避免斜杠被误编码
          const url = "/static/" + relPath.split("/").map(encodeURIComponent).join("/");
          const isPicked = pickedFilename === basename;
          return (
            <div
              key={relPath}
              onClick={() => onPickImage({ ...img, filename: basename })}
              style={{
                width: "100%",
                aspectRatio: "1/1",
                background: "#fafafa",
                cursor: "pointer",
                overflow: "hidden",
                borderRadius: 3,
                border: isPicked ? "3px solid #1677ff" : "1px solid #eee",
                position: "relative",
              }}
            >
              <img
                src={url}
                alt={basename}
                loading="lazy"
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                onError={(e) => { (e.target as HTMLImageElement).style.opacity = "0.2"; }}
              />
              {/* 🔍 放大预览按钮 — 半透明常显在左上，点击 stopPropagation 让"选中"不受影响 */}
              <div
                onClick={(e) => { e.stopPropagation(); onPreview(url); }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(0,0,0,0.85)"; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(0,0,0,0.5)"; }}
                title="放大预览"
                style={{
                  position: "absolute", top: 4, left: 4,
                  width: 22, height: 22, borderRadius: 4,
                  background: "rgba(0,0,0,0.5)", color: "#fff",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 12,
                  cursor: "zoom-in",
                  transition: "background 0.15s",
                  zIndex: 2,
                }}
              >
                🔍
              </div>
              {isPicked && (
                <div style={{
                  position: "absolute", top: 4, right: 4,
                  width: 20, height: 20, borderRadius: 10,
                  background: "#1677ff", color: "#fff",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 12,
                }}>✓</div>
              )}
              {/* 显示 image_tags 里的核心项做小标签 */}
              {img.tags && Object.keys(img.tags).length > 0 && (
                <div style={{
                  position: "absolute", bottom: 0, left: 0, right: 0,
                  padding: "2px 4px",
                  background: "linear-gradient(transparent, rgba(0,0,0,0.6))",
                  color: "#fff", fontSize: 9,
                  pointerEvents: "none",
                  textAlign: "center",
                }}>
                  {Object.values(img.tags).filter(Boolean).slice(0, 3).join(" · ")}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
