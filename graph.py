"""
LangGraph 状态图构建 — 将 5 个节点组装为客服工单处理流水线。

拓扑结构：
    classify_node → handle_node → decide_node（条件路由）
                                          ├─ escalate_reason 为空 → reply_auto_node → END
                                          └─ escalate_reason 非空 → escalate_node → END
"""

import json
import time
from typing import Any

from langgraph.graph import StateGraph, START, END

from state import TicketState
from nodes import (
    classify_node,
    handle_node,
    decide_node,
    reply_auto_node,
    escalate_node,
    reset_llm_counter,
    get_llm_call_count,
    get_llm_retries,
)
from logger import log_ticket


def _route_after_decide(state: TicketState) -> str:
    """条件路由：根据 decide_node 是否设置了 escalate_reason 决定下一步。"""
    if state.get("escalate_reason", ""):
        return "escalate"
    return "auto"


def build_graph() -> StateGraph:
    """构建并编译客服工单处理图。"""
    builder = StateGraph(TicketState)

    builder.add_node("classify", classify_node)
    builder.add_node("handle", handle_node)
    builder.add_node("decide", decide_node)
    builder.add_node("reply_auto", reply_auto_node)
    builder.add_node("escalate", escalate_node)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "handle")
    builder.add_edge("handle", "decide")
    builder.add_conditional_edges(
        "decide",
        _route_after_decide,
        {"auto": "reply_auto", "escalate": "escalate"},
    )
    builder.add_edge("reply_auto", END)
    builder.add_edge("escalate", END)

    return builder.compile()


_graph = build_graph()


def run_ticket(state: dict[str, Any]) -> dict[str, Any]:
    """运行一张工单的完整流水线，含结构化日志。

    每张工单调用前重置 LLM 计数器，执行后写入 logs/agent.log。
    """
    reset_llm_counter()
    result = _graph.invoke(state)
    llm_calls = get_llm_call_count()
    retries = get_llm_retries()
    log_ticket(result, {"llm_calls": llm_calls, "retries": retries})
    return result


# ── 执行链路采集（观测页用，结构与编排器项目保持一致）────────────

_FIELD_LABEL = {
    "category": "意图分类",
    "extracted_info": "提取信息",
    "tool_results": "工具执行",
    "escalate_reason": "转人工原因",
    "status": "工单状态",
    "resolution": "处理结果",
    "history": "用户历史",
    "risk_reasons": "风险信号",
    "risk_blocked": "写操作已拦下",
    "proposed_action": "待审批动作",
}


def _short(v: Any, n: int = 420) -> str:
    """把任意值压成可展示的短文本。"""
    if isinstance(v, str):
        s = v
    else:
        try:
            s = json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(v)
    return s[:n]


def _summarize_update(update: Any) -> list[dict[str, Any]]:
    """把一个节点的 state 更新压成可展示条目。

    items 结构与编排器项目一致：{"type", "name", "content"}，
    前端可复用同一套渲染逻辑。
    """
    if not isinstance(update, dict):
        return [{"type": "NodeOutput", "name": "", "content": _short(update)}]

    items: list[dict[str, Any]] = []
    for key, val in update.items():
        if key.startswith("_"):          # _timings 之类内部字段不展示
            continue
        label = _FIELD_LABEL.get(key, key)

        if key == "tool_results" and isinstance(val, list):
            for tr in val:
                if not isinstance(tr, dict):
                    continue
                items.append({
                    "type": "ToolResult",
                    "name": str(tr.get("tool_name", "tool")),
                    "ok": bool(tr.get("success")),
                    "content": _short(tr.get("result") or tr.get("error") or tr),
                })
        elif val not in ("", None, [], {}):
            items.append({"type": "StateField", "name": label, "content": _short(val)})

    return items or [{"type": "NodeOutput", "name": "", "content": "（本节点无状态更新）"}]


def run_ticket_with_steps(
    state: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """运行工单流水线，返回 (最终 state, 执行步骤列表)。

    与 run_ticket 的区别：改用 LangGraph 的 stream 逐节点消费，
    因此能拿到「分类 → 工具执行 → 风险决策 → 自动回复/转人工」每一跳的状态更新与耗时。
    """
    reset_llm_counter()
    t0 = time.time()
    prev = t0
    steps: list[dict[str, Any]] = []
    result: dict[str, Any] = dict(state)

    for chunk in _graph.stream(state, stream_mode="updates"):
        for node, update in chunk.items():
            now = time.time()
            steps.append({
                "node": node,
                "step_ms": int((now - prev) * 1000),
                "total_ms": int((now - t0) * 1000),
                "items": _summarize_update(update),
            })
            prev = now
            if isinstance(update, dict):
                result.update(update)

    llm_calls = get_llm_call_count()
    retries = get_llm_retries()
    log_ticket(result, {"llm_calls": llm_calls, "retries": retries})
    return result, steps
