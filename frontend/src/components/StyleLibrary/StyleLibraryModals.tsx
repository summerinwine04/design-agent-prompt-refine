/**
 * 款图库管理弹窗（docs/款图库管理_产品方案PRD.md v1.3）
 *
 * - StyleUploadModal ：新款入库 / 追加色号。每条 SKU = 色号+色名+正面图+背面图（正反面必填）；
 *   款号/色号留空自动生成（IT{YYMMDD}-NN / C 序号）。新款自动拼接款级双视角图。
 * - StyleLibraryModal：库列表 + 搜款号 + 品类筛选 + 徽标（缺双视角图/缺背面/待补录）+
 *   行内编辑品类 + 展开行补背面 / 用某色号重拼双视角图 / 直传双视角图。
 */

import {
  Alert, Button, Empty, Input, Modal, Popconfirm, Select, Space, Spin, Table, Tag, Upload, message,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  STYLE_CATEGORY_MAIN, STYLE_CATEGORY_SUB, StyleListItem,
  getStyle, listStyles, patchStyleMeta, updateStyleCover, uploadStyle, uploadStyleBack,
} from "../../api/client";

const subLabel = (v: string | null) =>
  STYLE_CATEGORY_SUB.find((s) => s.value === v)?.label || v || "";

const errText = (e: any) =>
  typeof e?.response?.data?.detail === "string" ? e.response.data.detail : e?.message || "未知错误";

// ============================================================================
// 上传弹窗
// ============================================================================

interface SkuDraft {
  code: string;
  name: string;
  front: File | null;
  back: File | null;
}

const emptySku = (): SkuDraft => ({ code: "", name: "", front: null, back: null });

export function StyleUploadModal({
  open, presetStyleNo, onClose, onDone,
}: {
  open: boolean;
  presetStyleNo?: string;           // 追加色号模式：预置款号
  onClose: () => void;
  onDone?: (styleNo: string) => void;
}) {
  const queryClient = useQueryClient();
  const { data: stylesData } = useQuery({ queryKey: ["styles"], queryFn: listStyles, enabled: open });

  const [styleNo, setStyleNo] = useState("");
  const [catMain, setCatMain] = useState<string | undefined>();
  const [catSub, setCatSub] = useState<string | undefined>();
  const [skus, setSkus] = useState<SkuDraft[]>([emptySku()]);
  const [submitting, setSubmitting] = useState(false);
  const [touched, setTouched] = useState(false);

  // 打开时应用预置款号（追加模式）
  const effStyleNo = (touched ? styleNo : presetStyleNo || styleNo).trim();
  const existing: StyleListItem | undefined = useMemo(
    () => (stylesData?.styles || []).find((s: StyleListItem) => s.style_no === effStyleNo),
    [stylesData, effStyleNo],
  );
  const isAppend = !!existing;

  // 追加模式下品类默认带出已有值
  const effCatMain = catMain ?? existing?.category_main ?? undefined;
  const effCatSub = catSub ?? existing?.category_sub ?? undefined;

  const reset = () => {
    setStyleNo(""); setCatMain(undefined); setCatSub(undefined);
    setSkus([emptySku()]); setTouched(false);
  };

  const setSku = (i: number, patch: Partial<SkuDraft>) =>
    setSkus((prev) => prev.map((s, j) => (j === i ? { ...s, ...patch } : s)));

  const canSubmit =
    !!effCatMain && !!effCatSub && skus.length > 0 &&
    skus.every((s) => s.name.trim() && s.front && s.back);

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const detail = await uploadStyle({
        style_no: effStyleNo || undefined,
        category_main: effCatMain!,
        category_sub: effCatSub!,
        skus: skus.map((s) => ({ code: s.code.trim(), name: s.name.trim(), front: s.front!, back: s.back! })),
      });
      message.success(
        isAppend
          ? `已为 ${detail.style_no} 追加 ${skus.length} 个色号`
          : `新款 ${detail.style_no} 已入库（含双视角图）`,
      );
      queryClient.invalidateQueries({ queryKey: ["styles"] });
      queryClient.invalidateQueries({ queryKey: ["style", detail.style_no] });
      onDone?.(detail.style_no);
      reset();
      onClose();
    } catch (e: any) {
      message.error("入库失败：" + errText(e));
    } finally {
      setSubmitting(false);
    }
  };

  const filePicker = (
    i: number, kind: "front" | "back", file: File | null,
  ) => (
    <Upload
      accept="image/jpeg,image/png,image/webp"
      showUploadList={false}
      beforeUpload={(f) => {
        if (f.size > 20 * 1024 * 1024) { message.error("图片超过 20MB 上限"); return Upload.LIST_IGNORE; }
        setSku(i, { [kind]: f } as Partial<SkuDraft>);
        return false;
      }}
    >
      <div style={{
        width: 84, height: 112, border: file ? "1px solid #d9d9d9" : "1px dashed #bbb",
        borderRadius: 6, cursor: "pointer", background: "#fafafa",
        display: "flex", alignItems: "center", justifyContent: "center", overflow: "hidden",
      }}>
        {file ? (
          <img src={URL.createObjectURL(file)} alt="" style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }} />
        ) : (
          <span style={{ fontSize: 11, color: "#999", textAlign: "center" }}>
            {kind === "front" ? "正面图 *" : "背面图 *"}
          </span>
        )}
      </div>
    </Upload>
  );

  return (
    <Modal
      open={open}
      title={isAppend ? `➕ 追加色号 —— ${effStyleNo}` : "⬆ 新款入库"}
      width={640}
      onCancel={() => { reset(); onClose(); }}
      okText={isAppend ? "追加入库" : "上传入库"}
      cancelText="取消"
      confirmLoading={submitting}
      okButtonProps={{ disabled: !canSubmit }}
      onOk={submit}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
          <div>
            <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>款号（item id）</div>
            <Input
              placeholder="留空自动生成 IT··"
              value={touched ? styleNo : (presetStyleNo || styleNo)}
              disabled={!!presetStyleNo}
              onChange={(e) => { setTouched(true); setStyleNo(e.target.value); }}
            />
          </div>
          <div>
            <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>大类 *</div>
            <Select
              style={{ width: "100%" }}
              placeholder="选大类"
              value={effCatMain}
              onChange={setCatMain}
              options={STYLE_CATEGORY_MAIN.map((v) => ({ value: v, label: v }))}
            />
          </div>
          <div>
            <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>小类 *</div>
            <Select
              style={{ width: "100%" }}
              placeholder="选小类"
              value={effCatSub}
              onChange={setCatSub}
              options={STYLE_CATEGORY_SUB.map((s) => ({ value: s.value, label: s.label }))}
            />
          </div>
        </div>

        {isAppend && (
          <Alert
            type="info" showIcon style={{ fontSize: 12 }}
            message={`该款已在库（${existing!.color_count} 色）——进入追加色号模式，不会改动已有双视角图`}
          />
        )}

        {/* SKU 列表 */}
        {skus.map((s, i) => (
          <div key={i} style={{ border: "1px solid #f0f0f0", borderRadius: 8, padding: 10 }}>
            <Space align="start" size={10} wrap>
              {filePicker(i, "front", s.front)}
              {filePicker(i, "back", s.back)}
              <Space direction="vertical" size={6}>
                <Input
                  style={{ width: 160 }} size="small"
                  placeholder="色号（留空自动 C 序号）"
                  value={s.code}
                  onChange={(e) => setSku(i, { code: e.target.value })}
                />
                <Input
                  style={{ width: 160 }} size="small"
                  placeholder="色名 *（如 浅天蓝）"
                  value={s.name}
                  onChange={(e) => setSku(i, { name: e.target.value })}
                />
                {skus.length > 1 && (
                  <Button size="small" danger type="text" onClick={() => setSkus((p) => p.filter((_, j) => j !== i))}>
                    删除此色
                  </Button>
                )}
              </Space>
            </Space>
          </div>
        ))}
        <Button size="small" onClick={() => setSkus((p) => [...p, emptySku()])}>＋ 再加一个色号</Button>

        {!isAppend && (
          <Alert
            type="info" showIcon style={{ fontSize: 12 }}
            message="每个色号需正面+背面两张图；第一个色号的正反面会自动拼接成款级双视角图（生图分析用）"
          />
        )}
      </Space>
    </Modal>
  );
}

// ============================================================================
// 库管理弹窗
// ============================================================================

export function StyleLibraryModal({
  open, onClose, onAppend,
}: {
  open: boolean;
  onClose: () => void;
  onAppend?: (styleNo: string) => void;   // 「追加色号」→ 由父组件打开上传弹窗
}) {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["styles"], queryFn: listStyles, enabled: open });

  const [kw, setKw] = useState("");
  const [mainFilter, setMainFilter] = useState<string>("all");
  const [subFilter, setSubFilter] = useState<string>("all");

  const styles: StyleListItem[] = data?.styles || [];
  const filtered = useMemo(() => styles.filter((s) => {
    if (kw.trim() && !s.style_no.toLowerCase().includes(kw.trim().toLowerCase())) return false;
    if (mainFilter !== "all" && s.category_main !== mainFilter) return false;
    if (subFilter !== "all" && s.category_sub !== subFilter) return false;
    return true;
  }), [styles, kw, mainFilter, subFilter]);

  const invalidate = (styleNo?: string) => {
    queryClient.invalidateQueries({ queryKey: ["styles"] });
    if (styleNo) queryClient.invalidateQueries({ queryKey: ["style", styleNo] });
  };

  const metaMut = useMutation({
    mutationFn: (p: { styleNo: string; main: string; sub: string }) =>
      patchStyleMeta(p.styleNo, { category_main: p.main, category_sub: p.sub }),
    onSuccess: (_d, p) => { message.success(`已更新 ${p.styleNo} 品类`); invalidate(p.styleNo); },
    onError: (e: any) => message.error("更新失败：" + errText(e)),
  });

  const columns = [
    {
      title: "款图", width: 72, dataIndex: "style_no", key: "thumb",
      render: (_: string, r: StyleListItem) => {
        const url = r.ref_image_url || r.cover_color_url;
        return url ? (
          <img src={url} alt="" style={{ width: 56, height: 56, objectFit: "contain", background: "#f5f5f5", borderRadius: 4 }} />
        ) : <span style={{ fontSize: 11, color: "#bbb" }}>无图</span>;
      },
    },
    {
      title: "款号", dataIndex: "style_no", key: "no",
      render: (v: string, r: StyleListItem) => (
        <Space direction="vertical" size={2}>
          <strong style={{ fontSize: 13 }}>{v}</strong>
          <Space size={4} wrap>
            {!r.ref_image && <Tag color="red" style={{ fontSize: 10, margin: 0 }}>缺双视角图</Tag>}
            {r.missing_back_count > 0 && (
              <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>缺背面 ×{r.missing_back_count}</Tag>
            )}
            {r.meta_missing && <Tag color="gold" style={{ fontSize: 10, margin: 0 }}>待补录</Tag>}
            {r.color_count === 0 && <Tag style={{ fontSize: 10, margin: 0 }}>无色号</Tag>}
          </Space>
        </Space>
      ),
    },
    {
      title: "品类", key: "cat", width: 210,
      render: (_: unknown, r: StyleListItem) => (
        <Space size={4}>
          <Select
            size="small" style={{ width: 92 }} placeholder="大类"
            value={r.category_main ?? undefined}
            options={STYLE_CATEGORY_MAIN.map((v) => ({ value: v, label: v }))}
            onChange={(v) => metaMut.mutate({ styleNo: r.style_no, main: v, sub: r.category_sub || "top" })}
          />
          <Select
            size="small" style={{ width: 88 }} placeholder="小类"
            value={r.category_sub ?? undefined}
            options={STYLE_CATEGORY_SUB.map((s) => ({ value: s.value, label: s.label }))}
            onChange={(v) => metaMut.mutate({ styleNo: r.style_no, main: r.category_main || STYLE_CATEGORY_MAIN[0], sub: v })}
          />
        </Space>
      ),
    },
    { title: "色号数", dataIndex: "color_count", key: "cc", width: 64 },
    {
      title: "上传时间", dataIndex: "created_at", key: "at", width: 150,
      render: (v: string | null) => v || <span style={{ color: "#bbb" }}>—</span>,
    },
    {
      title: "操作", key: "ops", width: 96,
      render: (_: unknown, r: StyleListItem) => (
        <Button size="small" type="link" onClick={() => onAppend?.(r.style_no)}>追加色号</Button>
      ),
    },
  ];

  return (
    <Modal open={open} onCancel={onClose} footer={null} width="82vw" title="🗂 款图库管理">
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          placeholder="搜款号" style={{ width: 180 }} allowClear
          onSearch={setKw} onChange={(e) => { if (!e.target.value) setKw(""); }}
        />
        <Select
          style={{ width: 110 }} value={mainFilter} onChange={setMainFilter}
          options={[{ value: "all", label: "全部大类" }, ...STYLE_CATEGORY_MAIN.map((v) => ({ value: v, label: v }))]}
        />
        <Select
          style={{ width: 110 }} value={subFilter} onChange={setSubFilter}
          options={[{ value: "all", label: "全部小类" }, ...STYLE_CATEGORY_SUB.map((s) => ({ value: s.value, label: s.label }))]}
        />
        <span style={{ fontSize: 12, color: "#999" }}>
          共 {filtered.length} 款
          {styles.some((s) => s.meta_missing) &&
            ` · 待补录 ${styles.filter((s) => s.meta_missing).length} 款`}
        </span>
      </Space>

      {isLoading ? <Spin style={{ margin: 32 }} /> : filtered.length === 0 ? (
        <Empty description="没有匹配的款" />
      ) : (
        <Table
          rowKey="style_no"
          size="small"
          columns={columns as any}
          dataSource={filtered}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          expandable={{
            expandedRowRender: (r: StyleListItem) => (
              <StyleColorsPanel styleNo={r.style_no} onChanged={() => invalidate(r.style_no)} />
            ),
          }}
        />
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- 展开行：色号明细

function StyleColorsPanel({ styleNo, onChanged }: { styleNo: string; onChanged: () => void }) {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["style", styleNo],
    queryFn: () => getStyle(styleNo),
  });

  const backMut = useMutation({
    mutationFn: (p: { code: string; file: File }) => uploadStyleBack(styleNo, p.code, p.file),
    onSuccess: (_d, p) => { message.success(`色号 ${p.code} 背面图已补`); refetch(); onChanged(); },
    onError: (e: any) => message.error("补背面失败：" + errText(e)),
  });

  const coverMut = useMutation({
    mutationFn: (p: { colorCode?: string; file?: File }) => updateStyleCover(styleNo, p),
    onSuccess: () => { message.success("双视角图已更新"); refetch(); onChanged(); },
    onError: (e: any) => message.error("更新双视角图失败：" + errText(e)),
  });

  if (isLoading) return <Spin size="small" style={{ margin: 12 }} />;
  const colors = data?.colors || [];

  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <Space wrap size={6}>
        <span style={{ fontSize: 12, color: "#666" }}>款级双视角图：</span>
        {data?.ref_image_url
          ? <img src={data.ref_image_url} alt="" style={{ height: 64, objectFit: "contain", background: "#f5f5f5", borderRadius: 4 }} />
          : <Tag color="red">缺失</Tag>}
        <Upload
          accept="image/jpeg,image/png,image/webp" showUploadList={false}
          beforeUpload={(f) => { coverMut.mutate({ file: f }); return false; }}
        >
          <Button size="small" loading={coverMut.isPending}>上传现成双视角图</Button>
        </Upload>
      </Space>

      <Space wrap size={10}>
        {colors.length === 0 && <span style={{ fontSize: 12, color: "#bbb" }}>该款还没有色号图</span>}
        {colors.map((c: any) => (
          <div key={c.filename} style={{ border: "1px solid #f0f0f0", borderRadius: 6, padding: 8, width: 168 }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
              {c.code} · {c.name}
              {!c.back_url && <Tag color="orange" style={{ fontSize: 10, marginLeft: 4 }}>缺背面</Tag>}
            </div>
            <Space size={4}>
              <img src={c.url} alt="" style={{ width: 64, height: 84, objectFit: "contain", background: "#f5f5f5", borderRadius: 4 }} />
              {c.back_url ? (
                <img src={c.back_url} alt="" style={{ width: 64, height: 84, objectFit: "contain", background: "#f5f5f5", borderRadius: 4 }} />
              ) : (
                <Upload
                  accept="image/jpeg,image/png,image/webp" showUploadList={false}
                  beforeUpload={(f) => { backMut.mutate({ code: c.code, file: f }); return false; }}
                >
                  <div style={{
                    width: 64, height: 84, border: "1px dashed #faad14", borderRadius: 4,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    fontSize: 10, color: "#faad14", cursor: "pointer", textAlign: "center",
                  }}>补背面图</div>
                </Upload>
              )}
            </Space>
            {c.back_url && (
              <Popconfirm
                title={`用 ${c.code} 的正反面重拼双视角图？`}
                onConfirm={() => coverMut.mutate({ colorCode: c.code })}
                okText="重拼" cancelText="取消"
              >
                <Button size="small" type="link" style={{ fontSize: 11, padding: 0, marginTop: 4 }}>
                  设为双视角图
                </Button>
              </Popconfirm>
            )}
          </div>
        ))}
      </Space>
    </Space>
  );
}
