"""
SSE 事件流工具

设计要点：
1. **事件缓冲 (replay buffer)**：订阅者可能晚于 orchestrator 启动，缓冲让新订阅者能 catch up
2. **TraceEvent → dict 适配**：orchestrator 的 TraceWriter 产出 dataclass，SSE 要 dict
3. **多订阅者**：同一个 run_id 允许多个 EventSource 同时订阅（不同 tab、debugger）
4. **完成后清理**：run_finished 后等 N 秒（让晚来的订阅者拿到完整流），再清缓冲
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from typing import Any, AsyncIterator


BUFFER_MAX_EVENTS = 5000          # 单 run 最多缓存事件数（防 stream_delta 把内存吃爆）
BUFFER_RETAIN_SECONDS = 60        # run 结束后保留缓冲多久


class RunEventBus:
    """单进程内的 run_id → 订阅者队列 + 缓冲 映射。

    线程安全说明：publish 可能从 orchestrator 工作线程调用（M3 引入了线程池跑 run），
    而消费端（sse_stream）跑在 FastAPI 主 event loop 上。所以入队必须用
    call_soon_threadsafe 跨线程派发到主 loop。
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._buffer: dict[str, list[dict]] = defaultdict(list)
        self._finished_at: dict[str, float] = {}
        # main event loop 引用——由 main.py 启动时调 set_main_loop 注入
        self._main_loop: asyncio.AbstractEventLoop | None = None

    def set_main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._main_loop = loop

    # ------------------- publish ------------------- #

    def publish(self, run_id: str, event: Any) -> None:
        """从 TraceWriter callback 调用。event 可以是 dict 或 TraceEvent dataclass。
        线程安全：从工作线程调用时通过 call_soon_threadsafe 转发到主 loop。"""
        if is_dataclass(event):
            event_dict = {k: v for k, v in asdict(event).items() if v is not None}
        elif isinstance(event, dict):
            event_dict = event
        else:
            return

        # 入缓冲（限长 ring buffer）——dict 操作本身在 CPython 线程安全
        buf = self._buffer[run_id]
        buf.append(event_dict)
        if len(buf) > BUFFER_MAX_EVENTS:
            del buf[: len(buf) - BUFFER_MAX_EVENTS]

        # 推给所有在线订阅者
        queues = list(self._queues.get(run_id, []))
        if queues:
            if self._main_loop and not self._is_main_loop():
                # 工作线程 → 用 call_soon_threadsafe 跨线程派发
                self._main_loop.call_soon_threadsafe(
                    self._dispatch_to_queues, queues, event_dict
                )
            else:
                # 已经在主 loop（或没设 main_loop 兜底）→ 直接 put_nowait
                self._dispatch_to_queues(queues, event_dict)

        if event_dict.get("event") == "run_finished":
            self._finished_at[run_id] = time.time()

    def _dispatch_to_queues(self, queues: list, event_dict: dict) -> None:
        for q in queues:
            try:
                q.put_nowait(event_dict)
            except asyncio.QueueFull:
                pass

    def _is_main_loop(self) -> bool:
        """当前是否运行在 main loop 上"""
        try:
            return asyncio.get_running_loop() is self._main_loop
        except RuntimeError:
            # 当前不在 event loop 上（typically 在工作线程裸调用）
            return False

    # ------------------- subscribe ------------------- #

    def subscribe(self, run_id: str) -> tuple[asyncio.Queue, list[dict]]:
        """订阅。返回 (新队列, 已有缓冲快照)——SSE 流先吐缓冲再进队列。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=BUFFER_MAX_EVENTS)
        self._queues[run_id].append(q)
        buf_snapshot = list(self._buffer.get(run_id, []))
        return q, buf_snapshot

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        if run_id in self._queues and q in self._queues[run_id]:
            self._queues[run_id].remove(q)
            if not self._queues[run_id]:
                del self._queues[run_id]

    # ------------------- 清理 ------------------- #

    def cleanup_finished(self) -> None:
        """清掉超过保留期的已完成 run 缓冲。"""
        now = time.time()
        stale = [rid for rid, t in self._finished_at.items() if now - t > BUFFER_RETAIN_SECONDS]
        for rid in stale:
            self._buffer.pop(rid, None)
            self._finished_at.pop(rid, None)


# 单例
event_bus = RunEventBus()


# ============================================================================
# SSE generator
# ============================================================================

async def sse_stream(run_id: str) -> AsyncIterator[bytes]:
    """异步生成器：先吐缓冲事件，然后进实时模式直到 run_finished。"""
    q, buf = event_bus.subscribe(run_id)
    try:
        # 1. 先把已缓冲的事件一次性吐出来（这是 catch-up 关键）
        for ev in buf:
            yield _format_sse(ev)
            if ev.get("event") == "run_finished":
                return

        # 2. 进实时模式
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # 心跳，防代理超时
                yield b": heartbeat\n\n"
                continue
            yield _format_sse(event)
            if event.get("event") == "run_finished":
                break
    finally:
        event_bus.unsubscribe(run_id, q)
        event_bus.cleanup_finished()


def _format_sse(event: dict) -> bytes:
    """把事件 dict 序列化为 SSE 协议字节。"""
    event_type = event.get("event", "message")
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


# ============================================================================
# 给 orchestrator 用的回调适配器
# ============================================================================

def make_trace_callback(run_id: str):
    """返回一个 (TraceEvent) -> None 的回调，直接接到 TraceWriter.external_callback。"""
    def _cb(event):
        event_bus.publish(run_id, event)
    return _cb
