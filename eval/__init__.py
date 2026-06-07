"""
prompt-refine-agent / eval

Step 2 输出指标评估模块。

设计原则（详见 README.md）：
  1. 量化先于精修
  2. 确定性指标优先（不依赖 LLM-as-judge）
  3. 同函数测两线（legacy 单 prompt 输出 与 agentic 输出 共用同一组 metric）
  4. 质量与成本同行
  5. 指标版本化（schema_version 字段标识）
"""

SCHEMA_VERSION = "1.0"

from .metrics import all_metrics
from .runner import eval_step2_json, write_csv
from .compare import compare_rows

__all__ = [
    "SCHEMA_VERSION",
    "all_metrics",
    "eval_step2_json",
    "write_csv",
    "compare_rows",
]
