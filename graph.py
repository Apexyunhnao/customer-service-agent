"""
LangGraph 状态图构建 — 将 5 个节点组装为客服工单处理流水线。

拓扑结构：
    classify_node → handle_node → decide_node（条件路由）
                                          ├─ escalate_reason 为空 → reply_auto_node → END
                                          └─ escalate_reason 非空 → escalate_node → END
"""

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
    log_ticket(result, {"llm_calls": llm_calls, "retries": 0})
    return result
