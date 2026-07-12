import { Button, Select, Form, Input, InputNumber, Space, Card, Alert, Tag, Tooltip, List, Checkbox, Modal, Switch, Upload, Progress, Segmented, message } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { listTrends, listStyles, listFixtures, listRuns, getStyle, createFixture, uploadTrend, subscribeTrendImport } from "../../api/client";
import { useRunStore } from "../../store/runStore";
import PatternLibraryPickerModal from "../PatternLibraryPickerModal/PatternLibraryPickerModal";


const STATUS_COLOR: Record<string, string> = {
  running: "processing",
  succeeded: "success",
  succeeded_with_audit_warnings: "warning",
  failed: "error",
};

/**
 * 左侧输入栏：选趋势 / 款号 / 性别比 / K，触发 POST /runs。
 *
 * Backend 接口要的字段（见 backend/schemas.py RunCreateRequest）：
 *   - trend_json_path  (str)
 *   - ref_image_path   (str)
 *   - color_folder     (str)
 *   - gender_ratio     (str)
 *   - num_designs_k    (int)
 *   - dry_run          (bool)
 *   - prompt_bundle    (default {})
 *
 * 关键：款图正面图 = ai-supply/款图/{style_no}.jpg，色号文件夹 = ai-supply/款图/{style_no}/
 * 选款号时自动联动 color_folder。
 */
export default function InputsPanel() {
  const { data: trends } = useQuery({ queryKey: ["trends"], queryFn: listTrends });
  const { data: styles } = useQuery({ queryKey: ["styles"], queryFn: listStyles });
  const { data: fixtures } = useQuery({ queryKey: ["fixtures"], queryFn: listFixtures });
  const startRun = useRunStore((s) => s.startRun);
  const loadRun = useRunStore((s) => s.loadRun);
  const currentRunId = useRunStore((s) => s.currentRunId);
  const isRunning = useRunStore((s) => s.isRunning);
  const error = useRunStore((s) => s.error);
  const promptOverrides = useRunStore((s) => s.promptOverrides);
  const clearPromptOverrides = useRunStore((s) => s.clearPromptOverrides);
  const overrideEntries = Object.entries(promptOverrides);

  // 历史 run 列表（最近 20 条）
  const { data: historyRuns } = useQuery({
    queryKey: ["runs-history"],
    queryFn: () => listRuns(),
    refetchInterval: 5000,    // 5 秒拉一次，新 run 自动出现
  });

  // M5.3: 多选状态（用于"对比"快捷入口）
  const [compareSelected, setCompareSelected] = useState<Set<string>>(new Set());
  const toggleCompare = (id: string) => {
    setCompareSelected((s) => {
      const ns = new Set(s);
      if (ns.has(id)) ns.delete(id);
      else ns.add(id);
      return ns;
    });
  };

  const [form] = Form.useForm();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  // 多样性避重：默认开启（查同款+同趋势最近 5 轮历史 → 注入 2.4/2.5 prompt 避免雷同）
  const [diversityAvoidHistory, setDiversityAvoidHistory] = useState(true);
  // 款级缓存：默认开启（同款复用 2.1 款式分析 / 2.2 颜色识别，跨趋势/跨模式生效）
  const [reuseStyleAnalysis, setReuseStyleAnalysis] = useState(true);

  // v5 设计模式：
  //   MULTI_TOPIC         多主题（经典）—— 每色号独立主题，发散设计
  //   SINGLE_TOPIC_STRONG 强单主题     —— 全部色号 = 同一母图案的 colorway 变体
  //   COLLECTION_2SKU     上下装成套   —— 用户预选 look（上装色 × 下装色），联合设计
  const [designMode, setDesignMode] = useState<string>("MULTI_TOPIC");
  // v6 输入源：Mode B / Mode C 支持从图库文件夹进（跳过 2.4s 主题选择）；Mode A 只支持 trend_report
  const [inputSource, setInputSource] = useState<"trend_report" | "pattern_library">("trend_report");
  // 图库输入源：用户在 PatternLibraryPickerModal 里选好的文件夹 + 1-3 张核心参考图
  const [patternLibraryPath, setPatternLibraryPath] = useState<string>("");
  const [patternLibrarySelectedFiles, setPatternLibrarySelectedFiles] = useState<string[]>([]);
  const [patternLibraryPickerOpen, setPatternLibraryPickerOpen] = useState<boolean>(false);
  // Mode C：用户预选的 look 配对。file = 色号图文件名（唯一标识，后端按它精确匹配——
  // 色号 code 可能重复：同一照片编号对应多个颜色），code/name 仅作展示。
  const [collectionLooks, setCollectionLooks] = useState<Array<{
    topFile: string; topCode: string; topName: string;
    bottomFile: string; bottomCode: string; bottomName: string;
  }>>([]);

  // Mode A/B：色号子集选择（null = 全部设计；数组 = 只设计选中的色号 code）
  const [pickedColors, setPickedColors] = useState<string[] | null>(null);
  // 应用夹具时跳过一次"切换款号重置选色"的 effect（否则恢复的选色被清掉）
  const skipColorResetRef = useRef(false);

  // 监听当前 style_no → 拿色号数（性别比 X:X:X 校验时要用）
  // React Query 同 key 会自动复用 StylePreview 的 cache，不会重复请求
  const currentStyleNo = Form.useWatch("style_no", form);
  const { data: currentStyleDetail } = useQuery({
    queryKey: ["style-detail", currentStyleNo],
    queryFn: () => getStyle(currentStyleNo),
    enabled: !!currentStyleNo && designMode !== "COLLECTION_2SKU",
  });

  // Mode A 只支持趋势报告输入源；切回 Mode A 时强制 input_source=trend_report
  useEffect(() => {
    if (designMode === "MULTI_TOPIC" && inputSource !== "trend_report") {
      setInputSource("trend_report");
      setPatternLibraryPath("");
      setPatternLibrarySelectedFiles([]);
    }
  }, [designMode, inputSource]);
  // 切换款号时重置色号子集选择（回到"全部设计"）；应用夹具恢复选色时跳过一次
  useEffect(() => {
    if (skipColorResetRef.current) {
      skipColorResetRef.current = false;
      return;
    }
    setPickedColors(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentStyleNo]);

  // 性别比加总的期望值：Mode A/B = 参与设计的色号数（子集选择生效）；Mode C = look 数
  const colorCount: number =
    designMode === "COLLECTION_2SKU"
      ? collectionLooks.length
      : (pickedColors !== null
          ? pickedColors.length
          : (currentStyleDetail?.colors?.length ?? 0));

  // Mode C：上下装两个款位
  const topStyleNo = Form.useWatch("top_style_no", form);
  const bottomStyleNo = Form.useWatch("bottom_style_no", form);
  const { data: topStyleDetail } = useQuery({
    queryKey: ["style-detail", topStyleNo],
    queryFn: () => getStyle(topStyleNo),
    enabled: !!topStyleNo && designMode === "COLLECTION_2SKU",
  });
  const { data: bottomStyleDetail } = useQuery({
    queryKey: ["style-detail", bottomStyleNo],
    queryFn: () => getStyle(bottomStyleNo),
    enabled: !!bottomStyleNo && designMode === "COLLECTION_2SKU",
  });

  // 款号选项里同时藏 ref_image_path + color_folder，选中时一并填
  const styleOptions = useMemo(
    () => (styles?.styles ?? []).map((s: any) => ({
      value: s.style_no,
      label: `${s.style_no}（${s.color_count} 色）`,
      ref_image: s.ref_image,
      color_folder: s.color_folder,
    })),
    [styles],
  );

  const trendOptions = useMemo(
    () => (trends?.trends ?? []).map((t: any) => ({
      value: t.json_path,
      label: `${t.name}${t.parsed ? "" : "（未解析）"}`,
      disabled: !t.parsed,
    })),
    [trends],
  );

  // 从 v4 字符串反解析回 "X:X:X" 给 form 用
  // 兼容多种历史格式：
  //   "男童 4 个 / 女童 4 个 / 中性 1 个" → "4:4:1"
  //   "男童 40% / 女童 40% / 中性 20%"   → 取百分比当数字 → "40:40:20"（但校验会因 sum ≠ 色号数失败，需要用户手动改）
  //   "男女比接近1:1"                    → 默认 ""（让用户重填）
  const parseGenderRatioToStr = (s: string | undefined | null): string => {
    if (typeof s !== "string") return "";
    // 先匹配"男童 N 个 / 女童 N 个 / 中性 N 个"
    const boy = s.match(/男童\s*(\d+)/)?.[1];
    const girl = s.match(/女童\s*(\d+)/)?.[1];
    const neutral = s.match(/中性\s*(\d+)/)?.[1];
    if (boy && girl && neutral) return `${boy}:${girl}:${neutral}`;
    // 兼容 "X:X:X" 直传
    const directMatch = s.match(/(\d+)\s*[:：]\s*(\d+)\s*[:：]\s*(\d+)/);
    if (directMatch) return `${directMatch[1]}:${directMatch[2]}:${directMatch[3]}`;
    return "";
  };

  // 应用一个 fixture 到表单（核心：把 ref_image_path + color_folder 反查回 style_no）
  const applyFixture = (fixture: any) => {
    if (!fixture) return;
    const mode = fixture.design_mode || "MULTI_TOPIC";
    setDesignMode(mode);

    if (mode === "COLLECTION_2SKU" && fixture.styles?.length) {
      const topSlot = fixture.styles.find((s: any) => s.role === "top");
      const bottomSlot = fixture.styles.find((s: any) => s.role === "bottom");
      const matchOpt = (slot: any) => styleOptions.find(
        (o: any) => o.ref_image === slot?.ref_image_path || o.color_folder === slot?.color_folder,
      );
      form.setFieldsValue({
        trend_json_path: fixture.trend_json_path,
        top_style_no: matchOpt(topSlot)?.value,
        bottom_style_no: matchOpt(bottomSlot)?.value,
        gender_ratio_str: parseGenderRatioToStr(fixture.gender_ratio) || "1:1:0",
        num_designs_k: fixture.num_designs_k,
      });
      setCollectionLooks((fixture.looks || []).map((lk: any) => ({
        // 夹具里存的成员引用即文件名（新）或 code（旧夹具），两者都能被后端解析
        topFile: lk.members?.top || "",
        topCode: lk.members?.top || "",
        topName: "",
        bottomFile: lk.members?.bottom || "",
        bottomCode: lk.members?.bottom || "",
        bottomName: "",
      })));
      message.success(`已应用成套夹具：${fixture.name}`);
      return;
    }

    const matched = styleOptions.find(
      (o: any) => o.ref_image === fixture.ref_image_path || o.color_folder === fixture.color_folder,
    );
    form.setFieldsValue({
      trend_json_path: fixture.trend_json_path,
      style_no: matched?.value,
      gender_ratio_str: parseGenderRatioToStr(fixture.gender_ratio) || "4:4:1",
      num_designs_k: fixture.num_designs_k,
    });
    // 恢复色号子集（skip 标志防止 style_no 变化的 effect 把它清掉）
    skipColorResetRef.current = true;
    setPickedColors(fixture.selected_colors?.length ? fixture.selected_colors : null);
    message.success(`已应用夹具：${fixture.name}`);
  };

  // 监听 ?fixture=<id> URL 参数，自动应用（从 /fixtures 页跳过来时用）
  useEffect(() => {
    const fid = searchParams.get("fixture");
    if (fid && fixtures?.length && styleOptions.length) {
      const f = fixtures.find((x: any) => x.id === fid);
      if (f) {
        applyFixture(f);
        // 应用完清 URL 参数避免重复触发
        searchParams.delete("fixture");
        setSearchParams(searchParams, { replace: true });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fixtures, styleOptions]);

  // 「存为夹具」Modal 状态
  const [saveFixtureOpen, setSaveFixtureOpen] = useState(false);
  const [saveFixtureForm] = Form.useForm();

  const createMut = useMutation({
    mutationFn: (payload: Record<string, unknown>) => createFixture(payload),
    onSuccess: (f) => {
      message.success(`已存为夹具：${f.name}`);
      setSaveFixtureOpen(false);
      saveFixtureForm.resetFields();
      queryClient.invalidateQueries({ queryKey: ["fixtures"] });
    },
    onError: (err: any) => {
      message.error("存夹具失败：" + (err?.response?.data?.detail || err?.message));
    },
  });

  // 弹"存为夹具"Modal 前校验表单是否填完
  const openSaveFixtureModal = async () => {
    try {
      // v6 图库输入源：不校验 trend_json_path，改为校验 pattern_library_selected_files 非空
      const trendField = inputSource === "pattern_library" ? [] : ["trend_json_path"];
      const fieldsToCheck = designMode === "COLLECTION_2SKU"
        ? [...trendField, "top_style_no", "bottom_style_no", "num_designs_k", "gender_ratio_str"]
        : [...trendField, "style_no", "num_designs_k", "gender_ratio_str"];
      await form.validateFields(fieldsToCheck);
      if (inputSource === "pattern_library" && patternLibrarySelectedFiles.length === 0) {
        message.warning("图库输入源请先选文件夹 + 勾 1-3 张核心参考图");
        return;
      }
      if (designMode === "COLLECTION_2SKU" && collectionLooks.length === 0) {
        message.warning("成套模式请先配至少 1 套 look 再保存夹具");
        return;
      }
      setSaveFixtureOpen(true);
    } catch {
      message.warning("请先填完上方表单再保存");
    }
  };

  const submitSaveFixture = () => {
    saveFixtureForm.validateFields().then((meta) => {
      const values = form.getFieldsValue();
      // v4：gender_ratio_str "X:X:X" → 解析后拼成自然语言传 backend
      const parts = String(values.gender_ratio_str || "").split(/[:：]/).map((p) => Number(p.trim()));
      const [b, g, n] = [parts[0] || 0, parts[1] || 0, parts[2] || 0];
      const sum = b + g + n;
      const unitLabel = designMode === "COLLECTION_2SKU" ? "look" : "色号";
      const genderRatio = `男童 ${b} 个 / 女童 ${g} 个 / 中性 ${n} 个（共 ${sum} ${unitLabel}；用户指定的是各类绝对数，不是百分比）`;

      if (designMode === "COLLECTION_2SKU") {
        const topOpt = styleOptions.find((o: any) => o.value === values.top_style_no);
        const bottomOpt = styleOptions.find((o: any) => o.value === values.bottom_style_no);
        if (!topOpt || !bottomOpt) {
          message.error("上下装款号信息异常，请重新选择");
          return;
        }
        createMut.mutate({
          id: `fx-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
          name: meta.name,
          description: meta.description || null,
          trend_json_path: values.trend_json_path,
          ref_image_path: topOpt.ref_image,
          color_folder: topOpt.color_folder,
          gender_ratio: genderRatio,
          num_designs_k: values.num_designs_k,
          selected_colors: null,
          design_mode: "COLLECTION_2SKU",
          styles: [
            { role: "top", ref_image_path: topOpt.ref_image, color_folder: topOpt.color_folder },
            { role: "bottom", ref_image_path: bottomOpt.ref_image, color_folder: bottomOpt.color_folder },
          ],
          looks: collectionLooks.map((lk, i) => ({
            name: `look-${String(i + 1).padStart(2, "0")}`,
            members: { top: lk.topFile || lk.topCode, bottom: lk.bottomFile || lk.bottomCode },
          })),
        });
        return;
      }

      const styleOpt = styleOptions.find((o: any) => o.value === values.style_no);
      if (!styleOpt) {
        message.error("款号信息异常，请重新选择");
        return;
      }
      createMut.mutate({
        id: `fx-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        name: meta.name,
        description: meta.description || null,
        trend_json_path: inputSource === "pattern_library" ? null : values.trend_json_path,
        ref_image_path: styleOpt.ref_image,
        color_folder: styleOpt.color_folder,
        gender_ratio: genderRatio,
        num_designs_k: values.num_designs_k,
        // v6 图库输入源
        design_mode: designMode,
        input_source: inputSource,
        pattern_library_path: inputSource === "pattern_library" ? patternLibraryPath : null,
        pattern_library_selected_files:
          inputSource === "pattern_library" ? patternLibrarySelectedFiles : null,
        selected_colors:
          pickedColors !== null && pickedColors.length < (currentStyleDetail?.colors?.length ?? 0)
            ? pickedColors : null,
      });
    });
  };

  // 「+ 新增趋势」Modal + 流水线进度
  const [addTrendOpen, setAddTrendOpen] = useState(false);
  const [trendName, setTrendName] = useState("");
  const [trendPdf, setTrendPdf] = useState<File | null>(null);
  const [importJob, setImportJob] = useState<{
    jobId: string;
    stage: string;
    current: number;
    total: number;
    chars?: number;
    error?: string;
    succeeded?: boolean;
  } | null>(null);
  const importEsRef = useRef<EventSource | null>(null);

  const resetTrendModal = () => {
    setAddTrendOpen(false);
    setTrendName("");
    setTrendPdf(null);
    setImportJob(null);
    if (importEsRef.current) {
      importEsRef.current.close();
      importEsRef.current = null;
    }
  };

  const handleStartImport = async () => {
    if (!trendName.trim()) {
      message.warning("请填写趋势报告名称");
      return;
    }
    if (!trendPdf) {
      message.warning("请选择 PDF 文件");
      return;
    }
    try {
      const resp = await uploadTrend(trendName.trim(), trendPdf);
      const jobId = resp.job_id;
      setImportJob({ jobId, stage: "starting", current: 0, total: 0 });
      // SSE 监听进度
      const es = subscribeTrendImport(jobId, (eventType, payload) => {
        if (eventType === "trend_progress" || eventType === "trend_started") {
          setImportJob((prev) => prev ? {
            ...prev,
            stage: payload.stage || prev.stage,
            current: payload.current ?? prev.current,
            total: payload.total ?? prev.total,
            chars: payload.chars ?? prev.chars,
          } : null);
        } else if (eventType === "trend_succeeded") {
          setImportJob((prev) => prev ? { ...prev, stage: "done", succeeded: true } : null);
          message.success(`趋势「${resp.name}」导入完成！`);
          queryClient.invalidateQueries({ queryKey: ["trends"] });
          es.close();
          importEsRef.current = null;
        } else if (eventType === "trend_failed") {
          setImportJob((prev) => prev ? { ...prev, stage: "failed", error: payload.error || "未知错误" } : null);
          es.close();
          importEsRef.current = null;
        }
      });
      importEsRef.current = es;
    } catch (err: any) {
      message.error("上传失败：" + (err?.response?.data?.detail || err?.message || String(err)));
    }
  };

  const submit = (dryRun: boolean) => {
    form.validateFields().then((values) => {
      // v4：性别比录入 "X:X:X" 表示 男:女:中性 各类色号数，加总 = 色号数（form validator 已校验）
      const parts = String(values.gender_ratio_str || "").split(/[:：]/).map((p) => Number(p.trim()));
      const [b, g, n] = [parts[0] || 0, parts[1] || 0, parts[2] || 0];
      const sum = b + g + n;
      const unitLabel = designMode === "COLLECTION_2SKU" ? "look" : "色号";
      const genderRatio = `男童 ${b} 个 / 女童 ${g} 个 / 中性 ${n} 个（共 ${sum} ${unitLabel}；用户指定的是各类绝对数，不是百分比）`;

      if (designMode === "COLLECTION_2SKU") {
        // Mode C：上下装成套 —— styles + 用户预选 looks
        const topOpt = styleOptions.find((o: any) => o.value === values.top_style_no);
        const bottomOpt = styleOptions.find((o: any) => o.value === values.bottom_style_no);
        if (!topOpt || !bottomOpt) return;
        if (collectionLooks.length === 0) {
          message.warning("请至少配一套 look（上装色 × 下装色）");
          return;
        }
        startRun({
          trend_json_path: values.trend_json_path,
          ref_image_path: topOpt.ref_image,        // primary 兜底字段
          color_folder: topOpt.color_folder,
          design_mode: "COLLECTION_2SKU",
          styles: [
            { role: "top", ref_image_path: topOpt.ref_image, color_folder: topOpt.color_folder },
            { role: "bottom", ref_image_path: bottomOpt.ref_image, color_folder: bottomOpt.color_folder },
          ],
          looks: collectionLooks.map((lk, i) => ({
            name: `look-${String(i + 1).padStart(2, "0")}`,
            // 成员引用用色号图文件名（唯一）；旧 code 引用后端仍兼容
            members: { top: lk.topFile || lk.topCode, bottom: lk.bottomFile || lk.bottomCode },
          })),
          gender_ratio: genderRatio,
          num_designs_k: values.num_designs_k,
          dry_run: dryRun,
          diversity_avoid_history: diversityAvoidHistory,
          reuse_style_analysis: reuseStyleAnalysis,
          prompt_bundle: { versions: {} },
        });
        return;
      }

      // Mode A / B：单款
      const styleOpt = styleOptions.find((o: any) => o.value === values.style_no);
      if (!styleOpt) return;
      if (pickedColors !== null && pickedColors.length === 0) {
        message.warning("至少选择 1 个色号参与设计");
        return;
      }
      // v6 图库输入源前置校验
      if (inputSource === "pattern_library") {
        if (!patternLibraryPath) {
          message.warning("请选择图库文件夹");
          return;
        }
        if (patternLibrarySelectedFiles.length === 0) {
          message.warning("请从图库中勾选 1-3 张核心参考图");
          return;
        }
      }
      const totalColors = currentStyleDetail?.colors?.length ?? 0;
      const payload: Record<string, unknown> = {
        // trend_json_path 在图库模式下不填（backend 会兜底 {}）
        trend_json_path: inputSource === "trend_report" ? values.trend_json_path : undefined,
        ref_image_path: styleOpt.ref_image,
        color_folder: styleOpt.color_folder,
        gender_ratio: genderRatio,
        num_designs_k: values.num_designs_k,
        dry_run: dryRun,
        diversity_avoid_history: diversityAvoidHistory,
        reuse_style_analysis: reuseStyleAnalysis,
        // 色号子集：全选传 null（走全量老行为），部分选传 code 列表
        selected_colors:
          pickedColors !== null && pickedColors.length < totalColors ? pickedColors : null,
        prompt_bundle: { versions: {} },
      };
      if (designMode === "SINGLE_TOPIC_STRONG") {
        payload.design_mode = "SINGLE_TOPIC_STRONG";
      }
      // v6：图库输入源塞进 payload（图库模式下无论 design_mode 都必须带）
      if (inputSource === "pattern_library") {
        payload.input_source = "pattern_library";
        payload.pattern_library_path = patternLibraryPath;
        payload.pattern_library_selected_files = patternLibrarySelectedFiles;
      }
      startRun(payload);
    }).catch(() => {
      // validateFields rejected (有空字段)，AntD 自动高亮，无需额外提示
    });
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {error && (
        <Alert
          type="error"
          message="启动失败"
          description={error}
          closable
          onClose={() => useRunStore.setState({ error: null })}
        />
      )}

      <Card size="small" title="输入">
        {/* v5 设计模式选择 */}
        <div style={{ marginBottom: 12 }}>
          <Segmented
            block
            size="small"
            value={designMode}
            disabled={isRunning}
            onChange={(v) => setDesignMode(String(v))}
            options={[
              { label: "🎨 多主题", value: "MULTI_TOPIC" },
              { label: "🧬 强单主题", value: "SINGLE_TOPIC_STRONG" },
              { label: "👕👖 上下装成套", value: "COLLECTION_2SKU" },
            ]}
          />
          <div style={{ fontSize: 11, color: "#999", marginTop: 4 }}>
            {designMode === "MULTI_TOPIC" && "经典模式：选 2-3 个趋势主题，每个色号独立设计"}
            {designMode === "SINGLE_TOPIC_STRONG" && "收敛模式：选 1 个主题 → 出母图案 Blueprint → 全部色号是同一张画的 colorway 变体"}
            {designMode === "COLLECTION_2SKU" && "成套模式：选上装 + 下装两个款，预先配好 look（上装色 × 下装色），每套联合设计、共用母图案"}
          </div>
        </div>

        {/* v6 输入源：仅 Mode B / Mode C 显示，Mode A 隐藏（强制 trend_report） */}
        {designMode !== "MULTI_TOPIC" && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 12, marginBottom: 4, color: "#666" }}>
              输入源
              <Tooltip title="图库输入：从「印花图案库」选一个方向文件夹 + 勾 1-3 张核心参考图 → 直接跳过主题选择，进 blueprint。图库模式下 blueprint 会看着这几张图抽象母图案 DNA">
                <span style={{ marginLeft: 4, color: "#999", cursor: "help" }}>?</span>
              </Tooltip>
            </div>
            <Segmented
              block
              size="small"
              value={inputSource}
              disabled={isRunning}
              onChange={(v) => setInputSource(v as any)}
              options={[
                { label: "📄 趋势报告", value: "trend_report" },
                { label: "🖼 图库文件夹", value: "pattern_library" },
              ]}
            />
          </div>
        )}

        <Form form={form} layout="vertical" size="small" disabled={isRunning}>
          {/* 趋势报告 —— 仅 trend_report 输入源显示 */}
          {inputSource === "trend_report" && (
            <Form.Item
              label={
                <Space style={{ width: "100%" }}>
                  <span>趋势报告</span>
                  <Button
                    size="small"
                    type="link"
                    style={{ padding: 0, fontSize: 12 }}
                    onClick={() => setAddTrendOpen(true)}
                    disabled={isRunning}
                  >
                    + 新增
                  </Button>
                </Space>
              }
              name="trend_json_path"
              rules={[{ required: inputSource === "trend_report", message: "请选择趋势报告" }]}
            >
              <Select placeholder="选趋势..." options={trendOptions} />
            </Form.Item>
          )}

          {/* 图库文件夹 —— 仅 pattern_library 输入源显示 */}
          {inputSource === "pattern_library" && (
            <Form.Item
              label="图库文件夹 + 核心参考图"
              required
              help={
                patternLibrarySelectedFiles.length > 0
                  ? `已选「${patternLibraryPath.split(/[/\\]/).pop() || patternLibraryPath}」的 ${patternLibrarySelectedFiles.length} 张作为核心参考`
                  : "点右侧按钮从图库中选一个方向文件夹 + 勾 1-3 张图作为母图案 blueprint 的视觉证据"
              }
              validateStatus={patternLibrarySelectedFiles.length === 0 ? "warning" : "success"}
            >
              <Space>
                <Button
                  onClick={() => setPatternLibraryPickerOpen(true)}
                  disabled={isRunning}
                  type={patternLibrarySelectedFiles.length === 0 ? "primary" : "default"}
                >
                  🖼 {patternLibrarySelectedFiles.length === 0 ? "选图库文件夹" : "改选图库"}
                </Button>
                {patternLibrarySelectedFiles.length > 0 && (
                  <span style={{ fontSize: 12, color: "#52c41a" }}>
                    ✓ {patternLibrarySelectedFiles.length}/3 张
                  </span>
                )}
              </Space>
            </Form.Item>
          )}

          {designMode !== "COLLECTION_2SKU" ? (
            <>
              <Form.Item
                label="款号"
                name="style_no"
                rules={[{ required: designMode !== "COLLECTION_2SKU", message: "请选择款号" }]}
                help="款图正面图 + 色号文件夹会自动联动"
              >
                <Select placeholder="选款号..." options={styleOptions} />
              </Form.Item>

              {/* 款号预览：选了款号后显示款图 + 一张色号图 */}
              <StylePreview formInstance={form} />

              {/* 色号子集选择：点色号图勾选参与设计的色号，默认全选 */}
              <ColorSubsetPicker
                styleDetail={currentStyleDetail}
                value={pickedColors}
                onChange={setPickedColors}
                disabled={isRunning}
              />
            </>
          ) : (
            <>
              <Form.Item
                label="上装款号"
                name="top_style_no"
                rules={[{ required: true, message: "请选择上装款号" }]}
              >
                <Select placeholder="选上装款号..." options={styleOptions} />
              </Form.Item>

              {/* 上装预览：与单款模式同款交互（款图 + 色号图可切换） */}
              <StylePreview formInstance={form} fieldName="top_style_no" label="👕 上装款图" />

              <Form.Item
                label="下装款号"
                name="bottom_style_no"
                rules={[
                  { required: true, message: "请选择下装款号" },
                  {
                    validator: (_r, v) =>
                      v && v === form.getFieldValue("top_style_no")
                        ? Promise.reject(new Error("上下装不能是同一个款"))
                        : Promise.resolve(),
                  },
                ]}
              >
                <Select placeholder="选下装款号..." options={styleOptions} />
              </Form.Item>

              {/* 下装预览 */}
              <StylePreview formInstance={form} fieldName="bottom_style_no" label="👖 下装款图" />

              {/* Look 预选：上装色 × 下装色 逐套确认 */}
              <LookBuilder
                topStyleDetail={topStyleDetail}
                bottomStyleDetail={bottomStyleDetail}
                looks={collectionLooks}
                onChange={setCollectionLooks}
                disabled={isRunning}
              />
            </>
          )}


          {/* v4：性别比用 X:X:X 格式直接录入色号数（男:女:中性），三数加总 = 当前款号色号数 */}
          <Form.Item
            label={
              <Space size={4}>
                <span>性别比</span>
                <span style={{ fontSize: 11, color: "#999" }}>
                  男:女:中性 · 三数加总 = {designMode === "COLLECTION_2SKU" ? "look 数" : "色号数"}{colorCount ? ` (${colorCount})` : ""}
                </span>
              </Space>
            }
            name="gender_ratio_str"
            initialValue="4:4:1"
            rules={[
              { required: true, message: "请输入 X:X:X" },
              {
                validator: (_rule, value) => {
                  if (!value) return Promise.reject(new Error("必填"));
                  const parts = String(value).trim().split(/[:：]/);
                  if (parts.length !== 3) {
                    return Promise.reject(new Error("格式：3 段数字用冒号分隔，如 4:4:1"));
                  }
                  const nums = parts.map((p) => Number(p.trim()));
                  if (nums.some((n) => !Number.isInteger(n) || n < 0)) {
                    return Promise.reject(new Error("每段必须是 ≥ 0 的整数"));
                  }
                  const sum = nums[0] + nums[1] + nums[2];
                  if (colorCount > 0 && sum !== colorCount) {
                    const unit = designMode === "COLLECTION_2SKU" ? "look 数" : "色号数";
                    return Promise.reject(new Error(`总和 ${sum} ≠ ${unit} ${colorCount}`));
                  }
                  return Promise.resolve();
                },
              },
            ]}
            extra={
              <div style={{ fontSize: 11, color: "#888" }}>
                直接录入各类色号数：如 <code>4:4:1</code> 表示 4 男童色号 / 4 女童色号 / 1 中性色号。
              </div>
            }
          >
            <Input
              placeholder={colorCount ? `如 ${Math.max(1, Math.floor(colorCount / 2))}:${Math.max(1, Math.floor((colorCount - 1) / 2))}:${colorCount - 2 * Math.max(1, Math.floor((colorCount - 1) / 2))}（共 ${colorCount} 色号）` : "如 4:4:1"}
              style={{ fontFamily: "ui-monospace, monospace" }}
            />
          </Form.Item>

          <Form.Item label="K（每色方案数）" name="num_designs_k" initialValue={1}>
            <InputNumber min={1} max={5} style={{ width: "100%" }} />
          </Form.Item>

          <Form.Item
            label={
              <Space size={4}>
                <span>🎲 历史避重</span>
                <Tooltip title="开启：查询同款+同趋势最近 5 轮 succeeded run，把选过的主题 & 同色号历史方案摘要注入 2.4 / 2.5 prompt，让 LLM 避免选到雷同主题、写出雷同图案。关掉可以还原原版发挥，用来做纯 prompt 迭代实验。">
                  <span style={{ color: "#999", fontSize: 12, cursor: "help" }}>?</span>
                </Tooltip>
              </Space>
            }
          >
            <Space size={8} align="center">
              <Switch
                checked={diversityAvoidHistory}
                onChange={setDiversityAvoidHistory}
                checkedChildren="ON"
                unCheckedChildren="OFF"
              />
              <span style={{ fontSize: 12, color: diversityAvoidHistory ? "#52c41a" : "#999" }}>
                {diversityAvoidHistory ? "查最近 5 轮历史 → 避免选同主题 & 写同图案" : "自由发挥，不查历史"}
              </span>
            </Space>
          </Form.Item>

          <Form.Item
            label={
              <Space size={4}>
                <span>♻️ 款级缓存</span>
                <Tooltip title="开启：同一个款复用之前 run 的 2.1 款式分析 + 2.2 颜色识别结果（跨趋势、跨设计模式生效），每次省约 10 次 LLM 调用。换款图/色号图、改 2.1/2.2 prompt、换模型会自动失效重跑。关掉用于 2.1/2.2 的纯净 prompt 实验。缓存可在「设置」页查看和清除。">
                  <span style={{ color: "#999", fontSize: 12, cursor: "help" }}>?</span>
                </Tooltip>
              </Space>
            }
            style={{ marginBottom: 8 }}
          >
            <Space size={8} align="center">
              <Switch
                checked={reuseStyleAnalysis}
                onChange={setReuseStyleAnalysis}
                checkedChildren="ON"
                unCheckedChildren="OFF"
              />
              <span style={{ fontSize: 12, color: reuseStyleAnalysis ? "#52c41a" : "#999" }}>
                {reuseStyleAnalysis ? "同款复用款式分析 & 颜色识别" : "每次全量重跑 2.1 / 2.2"}
              </span>
            </Space>
          </Form.Item>

          <Space wrap>
            <Button type="primary" onClick={() => submit(false)} loading={isRunning} disabled={isRunning}>
              ▶ 真跑（烧 token）
            </Button>
            <Button onClick={() => submit(true)} disabled={isRunning}>
              空跑（dry-run）
            </Button>
            <Tooltip title="把当前 4 个字段（趋势 / 款号 / 性别比 / K）冻结成可复用的夹具">
              <Button onClick={openSaveFixtureModal} disabled={isRunning}>
                💾 存为夹具
              </Button>
            </Tooltip>
          </Space>
        </Form>
      </Card>

      {overrideEntries.length > 0 && (
        <Card
          size="small"
          title={
            <Space>
              Prompt 版本覆盖
              <Tooltip title="下次提交会强制用这些版本。来自 PromptDrawer 的「用此版本跑」">
                <span style={{ color: "#999", fontSize: 12 }}>?</span>
              </Tooltip>
            </Space>
          }
          extra={
            <Button size="small" onClick={clearPromptOverrides} disabled={isRunning}>
              清除
            </Button>
          }
        >
          <Space wrap>
            {overrideEntries.map(([nodeBizId, version]) => (
              <Tag key={nodeBizId} color="purple">
                {nodeBizId} → {version}
              </Tag>
            ))}
          </Space>
        </Card>
      )}

      <Card
        size="small"
        title={
          <Space>
            历史 Run
            <Tooltip title="点条目本身加载进工作台；勾 checkbox 选 ≥2 个后点对比">
              <span style={{ color: "#999", fontSize: 12 }}>?</span>
            </Tooltip>
          </Space>
        }
        extra={
          compareSelected.size >= 2 ? (
            <Link to={`/compare?ids=${Array.from(compareSelected).join(",")}`}>
              <Button size="small" type="primary">
                对比 {compareSelected.size} 个 →
              </Button>
            </Link>
          ) : compareSelected.size === 1 ? (
            <span style={{ fontSize: 11, color: "#999" }}>再选 1 个</span>
          ) : null
        }
      >
        {!historyRuns || historyRuns.length === 0 ? (
          <div style={{ color: "#999", fontSize: 12 }}>暂无历史</div>
        ) : (
          <List
            size="small"
            dataSource={historyRuns.slice(0, 20)}
            renderItem={(r: any) => {
              const isActive = currentRunId === r.id;
              const isReal = !!r.total_tokens_in && r.total_tokens_in > 0;
              const hasFinal = r.status === "succeeded" || r.status === "succeeded_with_audit_warnings";
              const checked = compareSelected.has(r.id);
              return (
                <List.Item
                  style={{
                    cursor: "pointer",
                    background: isActive ? "#e6f4ff" : undefined,
                    padding: "4px 8px",
                  }}
                >
                  <Space direction="vertical" size={0} style={{ width: "100%" }}>
                    <Space size={4} style={{ width: "100%" }}>
                      <Tooltip title={hasFinal ? "勾选加入对比" : "未完成的 run 不能对比"}>
                        <Checkbox
                          checked={checked}
                          disabled={!hasFinal}
                          onChange={() => toggleCompare(r.id)}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Tooltip>
                      <div style={{ flex: 1, minWidth: 0 }} onClick={() => loadRun(r.id)}>
                        <Space size={6} style={{ fontSize: 11 }} wrap>
                          <span style={{ fontFamily: "ui-monospace, monospace", color: "#666" }}>
                            {r.id.slice(0, 17)}
                          </span>
                          <Tag color={STATUS_COLOR[r.status] || "default"} style={{ margin: 0 }}>
                            {r.audit_passed === true ? "✓审计" : r.audit_passed === false ? "⚠审计" : r.status}
                          </Tag>
                          {isReal && <Tag color="gold" style={{ margin: 0 }}>真跑</Tag>}
                          {!isReal && <Tag style={{ margin: 0 }}>dry</Tag>}
                          {r.design_mode === "SINGLE_TOPIC_STRONG" && <Tag color="purple" style={{ margin: 0 }}>🧬</Tag>}
                          {r.design_mode === "COLLECTION_2SKU" && <Tag color="purple" style={{ margin: 0 }}>👕👖</Tag>}
                        </Space>
                        <span style={{ fontSize: 11, color: "#999" }}>
                          {r.style_no} · {r.elapsed_ms ? `${(r.elapsed_ms / 1000).toFixed(0)}s` : "?"}
                          {r.total_tokens_in ? ` · ${((r.total_tokens_in + (r.total_tokens_out || 0)) / 1000).toFixed(1)}k tok` : ""}
                        </span>
                      </div>
                    </Space>
                  </Space>
                </List.Item>
              );
            }}
          />
        )}
      </Card>

      <Card
        size="small"
        title={`测试夹具 (${(fixtures ?? []).filter((f: any) => (f.design_mode || "MULTI_TOPIC") === designMode).length}/${fixtures?.length ?? 0})`}
        extra={<Link to="/fixtures" style={{ fontSize: 12 }}>管理 →</Link>}
      >
        {fixtures?.length ? (
          <Select
            style={{ width: "100%" }}
            placeholder={`选${designMode === "COLLECTION_2SKU" ? "成套" : designMode === "SINGLE_TOPIC_STRONG" ? "强单主题" : ""}夹具一键填表...`}
            allowClear
            disabled={isRunning}
            options={(fixtures ?? [])
              .filter((f: any) => (f.design_mode || "MULTI_TOPIC") === designMode)
              .map((f: any) => ({ value: f.id, label: f.name }))}
            onChange={(id) => {
              if (id) {
                const f = fixtures.find((x: any) => x.id === id);
                if (f) applyFixture(f);
              }
            }}
          />
        ) : (
          <div style={{ color: "#999", fontSize: 12 }}>
            还没有夹具。把上方表单填好后点「💾 存为夹具」可冻结一组输入下次复用。
          </div>
        )}
      </Card>

      {/* 「存为夹具」Modal */}
      <Modal
        title="💾 存为新夹具"
        open={saveFixtureOpen}
        onCancel={() => setSaveFixtureOpen(false)}
        onOk={submitSaveFixture}
        okText="保存"
        cancelText="取消"
        confirmLoading={createMut.isPending}
        destroyOnClose
      >
        <Form form={saveFixtureForm} layout="vertical" size="small" style={{ marginTop: 12 }}>
          <Form.Item
            label="夹具名称"
            name="name"
            rules={[{ required: true, message: "起个名字" }, { max: 40 }]}
          >
            <Input placeholder="如：YK250615 牛仔宽腿裤 · 男女均衡 · K=1" />
          </Form.Item>
          <Form.Item label="描述（可选）" name="description">
            <Input.TextArea rows={2} placeholder="用途、备注..." />
          </Form.Item>
          <Alert
            type="info"
            showIcon
            message="将保存当前 4 项："
            description={(() => {
              const v = form.getFieldsValue();
              const styleOpt = styleOptions.find((o: any) => o.value === v.style_no);
              return (
                <div style={{ fontSize: 12 }}>
                  <div>趋势：{v.trend_json_path?.split(/[\\/]/).pop() || "—"}</div>
                  <div>款号：{v.style_no || "—"}</div>
                  <div>色号文件夹：{styleOpt?.color_folder?.split(/[\\/]/).pop() || "—"}</div>
                  <div>性别比（男:女:中性）：{v.gender_ratio_str || "—"}</div>
                  <div>K：{v.num_designs_k || "—"}</div>
                </div>
              );
            })()}
            style={{ fontSize: 12 }}
          />
        </Form>
      </Modal>

      {/* 「+ 新增趋势」Modal —— 上传 PDF + name + 实时 SSE 进度 */}
      <Modal
        title="+ 新增趋势报告"
        open={addTrendOpen}
        onCancel={importJob && !importJob.succeeded && !importJob.error ? undefined : resetTrendModal}
        closable={!importJob || !!importJob.succeeded || !!importJob.error}
        maskClosable={false}
        footer={null}
        width={560}
        destroyOnClose
      >
        {!importJob ? (
          // 第 1 屏：表单（填 name + 选 PDF）
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Alert
              type="info"
              showIcon
              message="后端会自动跑 PDF → 图片 → LLM 解析 JSON"
              description="约 5-15 分钟，烧 OpenAI token（按你设置页的推理模型计费）。"
              style={{ fontSize: 12 }}
            />
            <div>
              <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>趋势报告名称 <span style={{ color: "red" }}>*</span></div>
              <Input
                placeholder="如：山系户外童装花型TOP热榜"
                value={trendName}
                onChange={(e) => setTrendName(e.target.value)}
                maxLength={120}
              />
              <div style={{ fontSize: 11, color: "#999", marginTop: 4 }}>
                将作为目录名 + JSON 文件名；特殊字符会被替换为 "_"。
              </div>
            </div>
            <div>
              <div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>PDF 文件 <span style={{ color: "red" }}>*</span></div>
              <Upload
                accept=".pdf"
                maxCount={1}
                fileList={trendPdf ? [{ uid: "1", name: trendPdf.name, status: "done" } as any] : []}
                beforeUpload={(file) => {
                  if (file.size > 200 * 1024 * 1024) {
                    message.error("PDF 超过 200MB 上限");
                    return Upload.LIST_IGNORE;
                  }
                  setTrendPdf(file);
                  return false; // 阻止自动上传，我们手动提交
                }}
                onRemove={() => { setTrendPdf(null); return true; }}
              >
                <Button>选 PDF 文件</Button>
              </Upload>
            </div>
            <Space style={{ width: "100%", justifyContent: "flex-end" }}>
              <Button onClick={resetTrendModal}>取消</Button>
              <Button
                type="primary"
                disabled={!trendName.trim() || !trendPdf}
                onClick={handleStartImport}
              >
                启动导入
              </Button>
            </Space>
          </Space>
        ) : (
          // 第 2 屏：进度面板
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <div>
              <div style={{ fontSize: 12, color: "#666" }}>Job ID：<code>{importJob.jobId}</code></div>
              <div style={{ fontSize: 13, marginTop: 4 }}>趋势：<strong>{trendName}</strong></div>
            </div>

            <ImportStageList stage={importJob.stage} current={importJob.current} total={importJob.total} chars={importJob.chars} />

            {importJob.error && (
              <Alert type="error" showIcon message="导入失败" description={importJob.error} />
            )}

            {importJob.succeeded && (
              <Alert
                type="success"
                showIcon
                message="导入完成！"
                description={`「${trendName}」已加入趋势报告下拉，可以选用。`}
              />
            )}

            <Space style={{ width: "100%", justifyContent: "flex-end" }}>
              {(importJob.succeeded || importJob.error) ? (
                <Button type="primary" onClick={resetTrendModal}>关闭</Button>
              ) : (
                <Tooltip title="导入中——关闭只会断开 SSE 显示，后台仍会跑完">
                  <Button onClick={resetTrendModal}>后台继续，关闭窗口</Button>
                </Tooltip>
              )}
            </Space>
          </Space>
        )}
      </Modal>

      {/* v6 图库文件夹选图 Modal */}
      <PatternLibraryPickerModal
        open={patternLibraryPickerOpen}
        onCancel={() => setPatternLibraryPickerOpen(false)}
        onConfirm={(folder, files) => {
          setPatternLibraryPath(folder);
          setPatternLibrarySelectedFiles(files);
          setPatternLibraryPickerOpen(false);
          message.success(`已选「${folder.split(/[/\\]/).pop() || folder}」的 ${files.length} 张作为核心参考图`);
        }}
        initialFolder={patternLibraryPath}
        initialFiles={patternLibrarySelectedFiles}
      />
    </Space>
  );
}

// 进度阶段列表（4 个阶段的可视化）
function ImportStageList({
  stage,
  current,
  total,
  chars,
}: { stage: string; current: number; total: number; chars?: number }) {
  // 阶段顺序：pdf2img → upload → llm_streaming → done
  const stages = [
    { key: "pdf2img", label: "1. PDF → 图片", showProgress: true },
    { key: "upload", label: "2. 上传图片到 OpenAI", showProgress: true, includes: ["upload", "upload_done"] },
    { key: "llm_streaming", label: "3. LLM 解析", showProgress: false, includes: ["llm_start", "llm_streaming"] },
    { key: "done", label: "4. 完成", showProgress: false, includes: ["done"] },
  ];
  const stageOrder = ["starting", "pdf2img", "upload", "upload_done", "llm_start", "llm_streaming", "done", "failed"];
  const currentIdx = stageOrder.indexOf(stage);

  return (
    <div>
      {stages.map((s) => {
        const inThisStage = s.key === stage || s.includes?.includes(stage);
        const passedIdx = stageOrder.indexOf(s.includes?.[s.includes.length - 1] || s.key);
        const isPassed = currentIdx > passedIdx;
        const isFailed = stage === "failed";
        return (
          <div key={s.key} style={{ marginBottom: 8 }}>
            <Space style={{ width: "100%", justifyContent: "space-between" }}>
              <span style={{ fontSize: 13, fontWeight: inThisStage ? 600 : 400, color: isFailed ? "#999" : undefined }}>
                {isPassed ? "✓" : inThisStage ? "▶" : "○"} {s.label}
              </span>
              {inThisStage && s.key === "llm_streaming" && (
                <span style={{ fontSize: 11, color: "#999" }}>已接收 {chars || 0} 字符</span>
              )}
              {inThisStage && s.showProgress && total > 0 && (
                <span style={{ fontSize: 11, color: "#999" }}>{current} / {total}</span>
              )}
            </Space>
            {inThisStage && s.showProgress && total > 0 && (
              <Progress
                percent={Math.round((current / total) * 100)}
                size="small"
                status={isFailed ? "exception" : "active"}
                style={{ marginTop: 2 }}
              />
            )}
            {inThisStage && s.key === "llm_streaming" && (
              <Progress
                percent={Math.min(99, Math.round(((chars || 0) / 10000) * 100))}
                size="small"
                status={isFailed ? "exception" : "active"}
                showInfo={false}
                style={{ marginTop: 2 }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}


/**
 * Mode A/B：色号子集选择器。
 * 展示选定款的全部色号图，点图勾选/取消（默认全选=全部设计）。
 * value: null = 全部；string[] = 选中的色号 code 列表。
 */
function ColorSubsetPicker({
  styleDetail, value, onChange, disabled,
}: {
  styleDetail: any;
  value: string[] | null;
  onChange: (v: string[] | null) => void;
  disabled?: boolean;
}) {
  const colors = styleDetail?.colors || [];
  if (!colors.length) return null;

  // 选中判定按文件名（唯一）；兼容老夹具存的 code 值（同码多色会一起匹配，交互一次后归一为文件名）
  const isSelected = (c: any): boolean =>
    value === null || value.includes(c.filename) || value.includes(c.code);
  const selCount = colors.filter(isSelected).length;
  const isAll = selCount >= colors.length;

  const toggle = (target: any) => {
    if (disabled) return;
    // 以文件名为准归一化当前选中集，再切换目标项
    const next = new Set(
      colors.filter((c: any) => isSelected(c) && c.filename !== target.filename)
            .map((c: any) => c.filename),
    );
    if (!isSelected(target)) next.add(target.filename);
    // 全选回 null（走全量老行为）
    onChange(next.size >= colors.length ? null : Array.from(next) as string[]);
  };

  return (
    <div style={{ marginTop: -4, marginBottom: 12, padding: 8, background: "#fafafa", borderRadius: 4 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <span style={{ fontSize: 12, color: "#666" }}>
          参与设计的色号（{selCount}/{colors.length}）
          <Tooltip title="点色号图勾选/取消。未勾选的色号不会进入设计（不烧 token 不生图）。默认全选。">
            <span style={{ color: "#999", marginLeft: 4, cursor: "help" }}>?</span>
          </Tooltip>
        </span>
        <Space size={4}>
          <Button size="small" type="link" style={{ fontSize: 11, padding: 0 }} disabled={disabled || isAll} onClick={() => onChange(null)}>
            全选
          </Button>
          <Button size="small" type="link" style={{ fontSize: 11, padding: 0 }} disabled={disabled || selCount === 0} onClick={() => onChange([])}>
            清空
          </Button>
        </Space>
      </div>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
        {colors.map((c: any) => {
          const isPicked = isSelected(c);
          return (
            <div
              key={c.filename || c.code}
              onClick={() => toggle(c)}
              title={`${c.code} · ${c.name}${isPicked ? "（已选）" : "（不设计）"}`}
              style={{
                width: 44, cursor: disabled ? "default" : "pointer", textAlign: "center",
                border: isPicked ? "2px solid #1677ff" : "2px solid transparent",
                borderRadius: 6, padding: 1, position: "relative",
                opacity: isPicked ? 1 : 0.45,
              }}
            >
              <img
                src={c.url}
                alt={c.name}
                style={{
                  width: 40, height: 40, objectFit: "cover", borderRadius: 4,
                  border: "1px solid #eee", background: "#fff",
                }}
                onError={(e: any) => { e.target.style.opacity = "0.3"; }}
              />
              {isPicked && (
                <span style={{
                  position: "absolute", top: 0, right: 0, background: "#1677ff",
                  color: "#fff", borderRadius: 6, fontSize: 8, padding: "0 3px", lineHeight: "12px",
                }}>✓</span>
              )}
              <div style={{
                fontSize: 9, color: isPicked ? "#1677ff" : "#bbb",
                whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
              }}>
                {c.name || c.code}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}


/**
 * v5 Mode C：Look 预选构建器。
 * 上装色下拉 × 下装色下拉 → 「+ 添加」逐套确认；已配 look 列表可删除。
 */
function LookBuilder({
  topStyleDetail,
  bottomStyleDetail,
  looks,
  onChange,
  disabled,
}: {
  topStyleDetail: any;
  bottomStyleDetail: any;
  looks: Array<{ topCode: string; topName: string; bottomCode: string; bottomName: string }>;
  onChange: (looks: Array<{ topCode: string; topName: string; bottomCode: string; bottomName: string }>) => void;
  disabled?: boolean;
}) {
  // pick 的值 = 色号图文件名（filename，唯一）。code 可能重复导致同码多图被一起高亮。
  const [topPick, setTopPick] = useState<string | undefined>();
  const [bottomPick, setBottomPick] = useState<string | undefined>();

  const topColors = topStyleDetail?.colors || [];
  const bottomColors = bottomStyleDetail?.colors || [];

  const urlOf = (colors: any[], file: string) =>
    colors.find((c: any) => c.filename === file)?.url;

  const addLook = () => {
    if (!topPick || !bottomPick) {
      message.warning("请先各点选一个上装色号图和下装色号图");
      return;
    }
    if (looks.some((l) => l.topFile === topPick && l.bottomFile === bottomPick)) {
      message.warning("这套配对已存在");
      return;
    }
    const t = topColors.find((c: any) => c.filename === topPick);
    const b = bottomColors.find((c: any) => c.filename === bottomPick);
    onChange([
      ...looks,
      {
        topFile: topPick,
        topCode: t?.code || topPick,
        topName: t?.name || "",
        bottomFile: bottomPick,
        bottomCode: b?.code || bottomPick,
        bottomName: b?.name || "",
      },
    ]);
    setTopPick(undefined);
    setBottomPick(undefined);
  };

  // 色号图可视化选择行：点图选中（蓝框高亮），再点取消
  const ColorPickRow = ({
    title, colors, picked, onPick, usedCodes,
  }: {
    title: string; colors: any[]; picked?: string;
    onPick: (code?: string) => void; usedCodes: Set<string>;
  }) => (
    <div style={{ marginBottom: 6 }}>
      <div style={{ fontSize: 11, color: "#888", marginBottom: 3 }}>{title}</div>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
        {colors.map((c: any) => {
          const isPicked = picked === c.filename;
          const isUsed = usedCodes.has(c.filename);
          return (
            <div
              key={c.filename}
              onClick={() => !disabled && onPick(isPicked ? undefined : c.filename)}
              title={`${c.code} · ${c.name}${isUsed ? "（已用于某套 look，可复用）" : ""}`}
              style={{
                width: 44, cursor: disabled ? "default" : "pointer", textAlign: "center",
                border: isPicked ? "2px solid #1677ff" : "2px solid transparent",
                borderRadius: 6, padding: 1, position: "relative",
              }}
            >
              <img
                src={c.url}
                alt={c.name}
                style={{
                  width: 40, height: 40, objectFit: "cover", borderRadius: 4,
                  border: "1px solid #eee", background: "#fff",
                  opacity: isUsed && !isPicked ? 0.75 : 1,
                }}
                onError={(e: any) => { e.target.style.opacity = "0.3"; }}
              />
              {isUsed && (
                <span style={{
                  position: "absolute", top: 0, right: 0, background: "#52c41a",
                  color: "#fff", borderRadius: 6, fontSize: 8, padding: "0 3px", lineHeight: "12px",
                }}>✓</span>
              )}
              <div style={{
                fontSize: 9, color: isPicked ? "#1677ff" : "#999",
                whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
              }}>
                {c.name || c.code}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );

  const usedTop = new Set(looks.map((l) => l.topFile));
  const usedBottom = new Set(looks.map((l) => l.bottomFile));

  return (
    <div style={{ marginTop: -4, marginBottom: 12, padding: 8, background: "#fafafa", borderRadius: 4 }}>
      <div style={{ fontSize: 12, color: "#666", marginBottom: 6 }}>
        Look 预选（{looks.length} 套）
        <Tooltip title="每套 = 上装一个色号 × 下装一个色号，点色号图选中后「组成一套」。设计阶段会对每套联合出方案（共用母图案、placement 互补）。未被任何 look 选中的色号不会设计。绿点表示该色号已被某套 look 使用（同一色号可复用于多套）。">
          <span style={{ color: "#999", marginLeft: 4, cursor: "help" }}>?</span>
        </Tooltip>
      </div>

      {(!topStyleDetail || !bottomStyleDetail) ? (
        <div style={{ fontSize: 11, color: "#999" }}>先选好上装和下装款号，再来配 look</div>
      ) : (
        <>
          <ColorPickRow
            title={`👕 上装色号（${topColors.length}）——点图选中`}
            colors={topColors}
            picked={topPick}
            onPick={setTopPick}
            usedCodes={usedTop}
          />
          <ColorPickRow
            title={`👖 下装色号（${bottomColors.length}）`}
            colors={bottomColors}
            picked={bottomPick}
            onPick={setBottomPick}
            usedCodes={usedBottom}
          />

          {/* 当前配对预览 + 组套按钮 */}
          <div style={{
            display: "flex", alignItems: "center", gap: 8, marginTop: 4,
            padding: 6, background: "#fff", border: "1px dashed #d9d9d9", borderRadius: 4,
          }}>
            <PairThumb url={topPick ? urlOf(topColors, topPick) : undefined} placeholder="👕" />
            <span style={{ color: "#bbb" }}>×</span>
            <PairThumb url={bottomPick ? urlOf(bottomColors, bottomPick) : undefined} placeholder="👖" />
            <Button
              size="small"
              type="primary"
              onClick={addLook}
              disabled={disabled || !topPick || !bottomPick}
              style={{ marginLeft: "auto" }}
            >
              ➕ 组成一套
            </Button>
          </div>

          {looks.length > 0 && (
            <div style={{ marginTop: 8 }}>
              {looks.map((lk, i) => (
                <div
                  key={`${lk.topFile}-${lk.bottomFile}`}
                  style={{
                    display: "flex", alignItems: "center", gap: 6,
                    fontSize: 11, padding: "3px 6px",
                    background: "#fff", border: "1px solid #eee", borderRadius: 4, marginBottom: 4,
                  }}
                >
                  <Tag color="blue" style={{ margin: 0 }}>look-{String(i + 1).padStart(2, "0")}</Tag>
                  <PairThumb url={urlOf(topColors, lk.topFile)} placeholder="👕" size={28} />
                  <span style={{ color: "#bbb" }}>×</span>
                  <PairThumb url={urlOf(bottomColors, lk.bottomFile)} placeholder="👖" size={28} />
                  <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "#666" }}>
                    {lk.topName || lk.topCode} × {lk.bottomName || lk.bottomCode}
                  </span>
                  <Button
                    size="small"
                    type="text"
                    danger
                    disabled={disabled}
                    style={{ height: 18, fontSize: 11, padding: "0 4px" }}
                    onClick={() => onChange(looks.filter((_, j) => j !== i))}
                  >
                    ✕
                  </Button>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}


/** 小方图缩略（look 配对里的上/下装色号图；无图时显示占位 emoji） */
function PairThumb({ url, placeholder, size = 36 }: { url?: string; placeholder: string; size?: number }) {
  if (!url) {
    return (
      <div style={{
        width: size, height: size, borderRadius: 4, border: "1px dashed #ddd",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: size * 0.5, background: "#fafafa", flexShrink: 0,
      }}>
        {placeholder}
      </div>
    );
  }
  return (
    <img
      src={url}
      style={{
        width: size, height: size, objectFit: "cover", borderRadius: 4,
        border: "1px solid #eee", background: "#fff", flexShrink: 0,
      }}
      onError={(e: any) => { e.target.style.opacity = "0.3"; }}
    />
  );
}


/**
 * 款号预览：选了款号后显示款图正面 + 一张色号图缩略
 * 用户可点切换查看不同色号
 * fieldName：监听的表单字段，默认 style_no（Mode C 传 top_style_no / bottom_style_no）
 * label：左侧图的标题，默认「款图正面」
 */
function StylePreview({
  formInstance,
  fieldName = "style_no",
  label = "款图正面",
}: {
  formInstance: any;
  fieldName?: string;
  label?: string;
}) {
  const styleNo = Form.useWatch(fieldName, formInstance);
  const [colorIdx, setColorIdx] = useState(0);

  // 切换款号时：色号索引归 0，并立即清空之前的预览（避免短暂闪现旧款图）
  useEffect(() => {
    setColorIdx(0);
  }, [styleNo]);

  const { data: styleDetail, isFetching } = useQuery({
    queryKey: ["style-detail", styleNo],
    queryFn: () => getStyle(styleNo),
    enabled: !!styleNo,
  });

  if (!styleNo) return null;

  // 切换款号期间（旧数据已无效、新数据还在拉）→ 显示骨架占位，不要继续显示旧 styleDetail
  // 因为 styleDetail 是上一个 styleNo 的缓存数据时切换会让用户以为"图没变"
  if (!styleDetail || isFetching) {
    return (
      <div style={{ marginTop: -8, marginBottom: 12, padding: 8, background: "#fafafa", borderRadius: 4, textAlign: "center", color: "#999", fontSize: 11 }}>
        预览加载中…
      </div>
    );
  }

  const colors = styleDetail.colors || [];
  const currentColor = colors[colorIdx % colors.length];

  return (
    <div style={{ marginTop: -8, marginBottom: 12, padding: 8, background: "#fafafa", borderRadius: 4 }}>
      <div style={{ fontSize: 11, color: "#999", marginBottom: 6 }}>预览</div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
        {/* 款图正面 */}
        <div style={{ textAlign: "center" }}>
          <img
            src={styleDetail.ref_image_url}
            alt={styleNo}
            style={{
              width: "100%",
              aspectRatio: "1",
              objectFit: "cover",
              borderRadius: 4,
              border: "1px solid #eee",
              background: "#fff",
            }}
            onError={(e: any) => { e.target.style.opacity = "0.3"; }}
          />
          <div style={{ fontSize: 10, color: "#888", marginTop: 2 }}>{label}</div>
        </div>

        {/* 色号图 */}
        {currentColor ? (
          <div style={{ textAlign: "center" }}>
            <img
              src={currentColor.url}
              alt={currentColor.name}
              style={{
                width: "100%",
                aspectRatio: "1",
                objectFit: "cover",
                borderRadius: 4,
                border: "1px solid #eee",
                background: "#fff",
              }}
              onError={(e: any) => { e.target.style.opacity = "0.3"; }}
            />
            <div style={{ fontSize: 10, color: "#888", marginTop: 2 }}>
              {currentColor.code} · <strong>{currentColor.name}</strong>
            </div>
          </div>
        ) : (
          <div style={{ textAlign: "center", color: "#999", fontSize: 11 }}>暂无色号</div>
        )}
      </div>

      {/* 色号切换 */}
      {colors.length > 1 && (
        <div style={{ display: "flex", justifyContent: "center", gap: 4, marginTop: 6 }}>
          <Button
            size="small"
            icon={<span>◀</span>}
            onClick={() => setColorIdx((i) => (i - 1 + colors.length) % colors.length)}
            style={{ height: 20, fontSize: 10 }}
          />
          <span style={{ fontSize: 10, color: "#999", alignSelf: "center" }}>
            {(colorIdx % colors.length) + 1} / {colors.length}
          </span>
          <Button
            size="small"
            icon={<span>▶</span>}
            onClick={() => setColorIdx((i) => (i + 1) % colors.length)}
            style={{ height: 20, fontSize: 10 }}
          />
        </div>
      )}
    </div>
  );
}
