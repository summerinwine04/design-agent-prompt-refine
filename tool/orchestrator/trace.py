"""
Trace 事件协议与磁盘缓存

每个节点（LLM / Tool / Decision / Loop）的生命周期事件统一通过 TraceEvent 描述，
并由 TraceWriter 同时落地到：
  - 内存 trace_tree（前端 SSE 订阅）
  - 磁盘 runs/{run_id}/trace.jsonl（每行一条事件，可重放）
  - 磁盘 runs/{run_id}/{node_id}_{slug}.json（每个 LLM/Tool 节点的 input+output 缓存，供 fork-from-node 复用）
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Literal


NodeType = Literal["llm", "tool", "decision", "loop", "run"]
NodeStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]


# --------------------------------------------------------------------------- #
# 事件 dataclass
# --------------------------------------------------------------------------- #

@dataclass
class TraceEvent:
    """
    单条 trace 事件。事件类型由 `event` 字段区分；不同事件类型用不同字段。

    事件类型与必含字段：
      - node_started      : node_id, parent_id, node_type, name, prompt_version?
      - node_streaming    : node_id, delta
      - node_completed    : node_id, output_summary?, tokens?, elapsed_ms?
      - node_failed       : node_id, error, retryable
      - decision_made     : node_id, condition, branch, reasoning
      - loop_iteration    : node_id, iter, total?, child_node_id
      - retry_triggered   : node_id, reason, new_node_id
      - run_started       : run_id, fixture, prompt_bundle
      - run_finished      : run_id, status, total_tokens, total_elapsed_ms
    """
    event: str
    timestamp: float = field(default_factory=time.time)
    node_id: str | None = None
    parent_id: str | None = None
    node_type: NodeType | None = None
    name: str | None = None
    prompt_version: str | None = None
    delta: str | None = None
    output_summary: dict[str, Any] | None = None
    tokens: dict[str, int] | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    retryable: bool | None = None
    condition: str | None = None
    branch: str | None = None
    reasoning: str | None = None
    iter: int | None = None
    total: int | None = None
    child_node_id: str | None = None
    reason: str | None = None
    new_node_id: str | None = None
    run_id: str | None = None
    fixture: dict[str, Any] | None = None
    prompt_bundle: dict[str, str] | None = None
    status: NodeStatus | None = None
    total_tokens: dict[str, int] | None = None
    total_elapsed_ms: int | None = None

    def to_json_line(self) -> str:
        d = {k: v for k, v in asdict(self).items() if v is not None}
        return json.dumps(d, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# TraceWriter — 单 run 的事件管道
# --------------------------------------------------------------------------- #

class TraceWriter:
    """
    单次 step2 run 的 trace 管道。同时：
      1) 把每条事件追加到 trace.jsonl
      2) 把每个 LLM / Tool 节点的 input+output 落到独立 JSON 文件（供 fork-from-node）
      3) 推给外部 callback（SSE 发射器 / 控制台日志）

    用法（async with 或手动 enter / exit）：
        writer = TraceWriter(run_dir=Path("runs/xxx"))
        writer.emit_run_started(run_id, fixture, prompt_bundle)
        async with writer.node("2.1", "款式分析", node_type="llm", prompt_version="01@v3") as n:
            ...
            n.set_output({"款图底色": ...})
        writer.emit_run_finished(status="succeeded", total_tokens={...}, total_elapsed_ms=...)
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        external_callback: Callable[[TraceEvent], None] | None = None,
        verbose: bool = False,
    ):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace_log = self.run_dir / "trace.jsonl"
        self.callback = external_callback
        self.verbose = verbose
        self._node_counter = 0
        self._totals = {"input_tokens": 0, "output_tokens": 0}
        self._t0 = time.time()

    # ------------------- 写入 ------------------- #

    # 高频事件不推 SSE callback——防止流式输出 token 雪崩压垮前端
    # （仍会写到 trace.jsonl，供 GET /runs/:id 全量查询用）
    _HIGH_FREQ_EVENTS = {"node_streaming"}

    def _write(self, ev: TraceEvent) -> None:
        with open(self.trace_log, "a", encoding="utf-8") as f:
            f.write(ev.to_json_line() + "\n")
        # 高频事件不进 callback（SSE）；只落盘
        if self.callback and ev.event not in self._HIGH_FREQ_EVENTS:
            try:
                self.callback(ev)
            except Exception as e:
                if self.verbose:
                    print(f"[trace callback error] {e}")
        if self.verbose:
            short = ev.event
            if ev.name:
                short += f" {ev.name}"
            if ev.node_id:
                short += f" ({ev.node_id})"
            print(f"  [trace] {short}")

    def _new_node_id(self, prefix: str) -> str:
        self._node_counter += 1
        return f"{prefix}_{self._node_counter:03d}"

    # ------------------- 事件 ------------------- #

    def emit_run_started(
        self, run_id: str, fixture: dict, prompt_bundle: dict[str, str]
    ) -> None:
        self._write(TraceEvent(
            event="run_started",
            run_id=run_id,
            fixture=fixture,
            prompt_bundle=prompt_bundle,
        ))

    def emit_run_finished(
        self,
        status: NodeStatus,
        *,
        total_tokens: dict | None = None,
        total_elapsed_ms: int | None = None,
    ) -> None:
        self._write(TraceEvent(
            event="run_finished",
            status=status,
            total_tokens=total_tokens or dict(self._totals),
            total_elapsed_ms=total_elapsed_ms or int((time.time() - self._t0) * 1000),
        ))

    def emit_decision(
        self,
        *,
        node_id: str,
        parent_id: str | None,
        condition: str,
        branch: str,
        reasoning: str,
    ) -> None:
        self._write(TraceEvent(
            event="decision_made",
            node_id=node_id,
            parent_id=parent_id,
            node_type="decision",
            condition=condition,
            branch=branch,
            reasoning=reasoning,
        ))

    def add_tokens(self, in_tokens: int = 0, out_tokens: int = 0) -> None:
        self._totals["input_tokens"] += in_tokens
        self._totals["output_tokens"] += out_tokens

    # ------------------- node 上下文管理器 ------------------- #

    def node(
        self,
        node_id_prefix: str,
        name: str,
        *,
        node_type: NodeType,
        parent_id: str | None = None,
        prompt_version: str | None = None,
    ) -> "NodeContext":
        return NodeContext(
            writer=self,
            node_id=self._new_node_id(node_id_prefix),
            name=name,
            node_type=node_type,
            parent_id=parent_id,
            prompt_version=prompt_version,
        )

    # ------------------- 节点缓存 ------------------- #

    def cache_node_io(self, node_id: str, slug: str, payload: dict) -> Path:
        """把节点的 input+output 落盘，供 fork-from-node 复用。"""
        cache_path = self.run_dir / f"{node_id}_{slug}.json"
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return cache_path


class NodeContext:
    """
    一个节点的执行作用域。用法：
        with writer.node("2.1", "款式分析", node_type="llm") as n:
            n.set_input({"系统prompt": "...", "user prompt": "..."})
            ... 执行 ...
            n.set_output({"款图底色": ...})
            n.set_tokens(in=..., out=...)
    """

    def __init__(
        self,
        writer: TraceWriter,
        node_id: str,
        name: str,
        node_type: NodeType,
        parent_id: str | None,
        prompt_version: str | None,
    ):
        self.writer = writer
        self.node_id = node_id
        self.name = name
        self.node_type = node_type
        self.parent_id = parent_id
        self.prompt_version = prompt_version
        self._t0 = 0.0
        self._input: dict = {}
        self._output: dict = {}
        self._tokens: dict = {}

    def __enter__(self) -> "NodeContext":
        self._t0 = time.time()
        self.writer._write(TraceEvent(
            event="node_started",
            node_id=self.node_id,
            parent_id=self.parent_id,
            node_type=self.node_type,
            name=self.name,
            prompt_version=self.prompt_version,
        ))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed_ms = int((time.time() - self._t0) * 1000)
        if exc_type is not None:
            self.writer._write(TraceEvent(
                event="node_failed",
                node_id=self.node_id,
                error=f"{exc_type.__name__}: {exc_val}",
                retryable=False,
                elapsed_ms=elapsed_ms,
            ))
            # 失败也缓存现场
            self.writer.cache_node_io(self.node_id, self._slug(), {
                "name": self.name,
                "node_type": self.node_type,
                "prompt_version": self.prompt_version,
                "input": self._input,
                "output": self._output,
                "tokens": self._tokens,
                "elapsed_ms": elapsed_ms,
                "status": "failed",
                "error": f"{exc_type.__name__}: {exc_val}",
            })
            return False  # re-raise
        else:
            self.writer._write(TraceEvent(
                event="node_completed",
                node_id=self.node_id,
                output_summary=_summarize_for_event(self._output),
                tokens=self._tokens or None,
                elapsed_ms=elapsed_ms,
            ))
            self.writer.cache_node_io(self.node_id, self._slug(), {
                "name": self.name,
                "node_type": self.node_type,
                "prompt_version": self.prompt_version,
                "input": self._input,
                "output": self._output,
                "tokens": self._tokens,
                "elapsed_ms": elapsed_ms,
                "status": "succeeded",
            })
            if self._tokens:
                self.writer.add_tokens(
                    self._tokens.get("input", 0), self._tokens.get("output", 0)
                )
            return False

    def _slug(self) -> str:
        return "".join(
            c if c.isalnum() or c in "_-" else "_"
            for c in self.name
        )

    # ------------- 业务方法 ------------- #

    def set_input(self, data: dict) -> None:
        self._input = data

    def set_output(self, data: dict) -> None:
        self._output = data

    def set_tokens(self, *, input: int = 0, output: int = 0) -> None:
        self._tokens = {"input": input, "output": output}

    def emit_streaming(self, delta: str) -> None:
        self.writer._write(TraceEvent(
            event="node_streaming",
            node_id=self.node_id,
            delta=delta,
        ))


# --------------------------------------------------------------------------- #
# 工具：把大输出做 summary 不灌到事件流里
# --------------------------------------------------------------------------- #

def _summarize_for_event(output: dict) -> dict:
    """把节点 output 做摘要，避免 SSE 事件包含巨型 JSON。"""
    if not output:
        return {}
    summary: dict = {}
    for key, value in output.items():
        if isinstance(value, str):
            summary[key] = value[:200] + ("..." if len(value) > 200 else "")
        elif isinstance(value, list):
            summary[key] = f"<list len={len(value)}>"
        elif isinstance(value, dict):
            summary[key] = f"<dict keys={list(value.keys())[:5]}>"
        else:
            summary[key] = value
    return summary


# --------------------------------------------------------------------------- #
# 工具：生成 run_id
# --------------------------------------------------------------------------- #

def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
