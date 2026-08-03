"""
结构化日志 — 每张工单一条 JSON 日志写入 logs/agent.log，
记录分类、工具调用、LLM 调用次数、重试、每步耗时，用于排查和审计。
"""

import json
import logging
import time
import os
from pathlib import Path
from datetime import datetime
from typing import Any

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "agent.log"

os.makedirs(LOG_DIR, exist_ok=True)

_logger = logging.getLogger("agent")
_logger.setLevel(logging.INFO)
_logger.handlers.clear()

_handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(message)s"))
_logger.addHandler(_handler)
_logger.propagated = False


class StepTimer:
    """单步计时器，用于节点耗时统计。"""

    def __init__(self) -> None:
        self._start = time.time()

    def elapsed_ms(self) -> int:
        """返回从创建到现在的毫秒数。"""
        return int((time.time() - self._start) * 1000)


def log_ticket(state: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    """写一条工单处理日志（一行 JSON）。

    Args:
        state: 图执行完成后的完整 state
        extra: 额外字段，如 llm_calls、retries
    """
    tool_results = state.get("tool_results", [])
    tools_summary = [
        {
            "tool": r.get("tool_name", "?"),
            "success": r.get("success"),
            "message": (r.get("message", "") or "")[:100],
        }
        for r in tool_results
    ]

    timings = state.get("_timings", {})

    entry: dict[str, Any] = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "ticket_id": state.get("ticket_id", ""),
        "category": state.get("category", ""),
        "status": state.get("status", ""),
        "escalate_reason": (state.get("escalate_reason", "") or "")[:200],
        "tools": tools_summary,
        "timings_ms": timings,
        "total_ms": sum(timings.values()) if timings else 0,
    }
    if extra:
        entry["llm_calls"] = extra.get("llm_calls", 0)
        entry["retries"] = extra.get("retries", 0)

    _logger.info(json.dumps(entry, ensure_ascii=False))
