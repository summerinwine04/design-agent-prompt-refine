"""
M6 生图任务核心：复用 ai-supply step3 的核心算法，重构成 backend 可调用模块。

主入口：
    from generator import GenerationRunner
    runner = GenerationRunner(...)
    runner.run()    # 阻塞，会发 SSE 事件
"""

from .runner import GenerationRunner

__all__ = ["GenerationRunner"]
