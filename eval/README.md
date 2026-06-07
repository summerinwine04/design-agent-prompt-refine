# eval — Step 2 输出指标评估模块

## 设计原则

1. **量化先于精修**：每次 prompt 改动必须有数字对照
2. **确定性指标优先**：能用 Python 算的，不让 LLM 评分
3. **同函数测两线**：legacy 单 prompt 输出 与 agentic 输出 共用同一组 metric
4. **质量与成本同行**：token / 耗时 / 调用次数 与质量指标同一行
5. **指标版本化**：`SCHEMA_VERSION` 字段标识；改了 metric 定义要升版本号

## 6 类指标

- **A 结构完整性** —— 方案完成率（K×N 应得 vs 实得）
- **B 审计四项** —— 复用 `tool/orchestrator/audit.py` 的 4 项硬性规则判定
- **C 字段填充质量** —— 锚点 face/tier 双坐标、七段式 prompt、≤50 字方案说明
- **D 比例合规** —— 性别分配 vs 用户要求、变色数量
- **E 质量（弱信号）** —— 模型自评适配度（参考用，**不作主决策**）
- **F 成本** —— input/output tokens、耗时、LLM 调用次数

## 使用

```bash
# 1. 对单份 step2 final JSON 跑指标
python scripts/eval.py runs/山系户外_YK250609.json

# 2. 横向对比多个 run
python scripts/eval.py \
    ../ai-supply/trend2design/山系户外_YK250609.json \
    runs/山系户外_YK250609_agentic_v1.json \
    --label legacy --label agentic_v1

# 3. 落 CSV
python scripts/eval.py runs/*.json --csv data/eval_results.csv

# 4. 只看有变化的指标
python scripts/eval.py legacy.json agentic.json --only-diff
```

## 新增 metric

在 `eval/metrics.py` 加一个新函数（注意前缀字母 `A`/`B`/.../`F`），追加到 `all_metrics`。
新 metric 不会破老数据——CSV 列自动 union，老 row 该列为空。

如果改了已有 metric 的语义（比如把 ≤50 字改成 ≤80 字），**必须升 `SCHEMA_VERSION`**，
后续对比时按 schema_version 隔离老数据。
