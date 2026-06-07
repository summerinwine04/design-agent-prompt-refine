"""
prompt-refine-agent — Step 2 Agentic Orchestrator

把单 prompt 的 step2_design 拆成 7 个 trace 节点：
  2.1 款式分析            (LLM)
  2.2 颜色识别 x N         (LLM, 并发)
  2.3 性别比规划           (LLM)
  2.4 单色设计 x N         (LLM, 串行 + 累积状态)
  2.5 分布审计             (Python tool, 0 token)
  2.6 重做决策             (Decision)
  2.7 局部重设计 (条件)     (LLM loop)

主入口：from orchestrator import Step2Run
CLI 入口：prompt-refine-agent/run_agent.py
"""

from .orchestrator import Step2Run
from .trace import TraceEvent, TraceWriter
from .audit import run_audits, AuditResult
from .prompts import PromptBundle, load_prompt_file

__all__ = [
    "Step2Run",
    "TraceEvent",
    "TraceWriter",
    "run_audits",
    "AuditResult",
    "PromptBundle",
    "load_prompt_file",
]
