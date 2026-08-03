"""
图节点实现 — 5 个节点函数，每个接收完整 state 返回部分更新的 dict。
节点之间通过 LangGraph StateGraph 按拓扑顺序调用。
"""

import json
import os
import re
import time
from typing import Any
from dotenv import load_dotenv

load_dotenv()  # 从项目根目录 .env 加载环境变量

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from state import TicketState
from tools import (
    query_order,
    query_logistics,
    update_address,
    refund_price_diff,
    update_remark,
    urge_delivery,
    process_refund,
    process_exchange,
)

# ── LLM 实例（延迟初始化）────────────────────────────────────────

_LLM: ChatOpenAI | None = None


_llm_call_count = 0  # 当前工单的 LLM 调用次数，graph 开始时重置


def _get_llm() -> ChatOpenAI:
    """延迟创建 LLM 实例，每次调用递增计数器。"""
    global _LLM, _llm_call_count
    _llm_call_count += 1
    if _LLM is None:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量，无法调用 DeepSeek API")
        _LLM = ChatOpenAI(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            api_key=api_key,
            temperature=0.1,
        )
    return _LLM


def reset_llm_counter() -> None:
    """重置 LLM 调用计数器（每张新工单开始时调用）。"""
    global _llm_call_count
    _llm_call_count = 0


def get_llm_call_count() -> int:
    """获取当前工单的 LLM 调用次数。"""
    return _llm_call_count


def _timing(state: dict, node_name: str, start: float) -> dict:
    """记录节点耗时到 state['_timings']，返回更新的 dict。"""
    elapsed = int((time.time() - start) * 1000)
    timings = state.get("_timings", {})
    timings[node_name] = elapsed
    return {"_timings": timings}


# ── JSON 解析辅助 ───────────────────────────────────────────────

def _parse_json_from_response(text: str) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON，兼容 markdown 代码块包裹的情况。"""
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ── 工具映射 ────────────────────────────────────────────────────

_TOOL_MAP: dict[str, Any] = {
    "query_order": query_order,
    "query_logistics": query_logistics,
    "update_address": update_address,
    "refund_price_diff": refund_price_diff,
    "update_remark": update_remark,
    "urge_delivery": urge_delivery,
    "process_refund": process_refund,
    "process_exchange": process_exchange,
}

_TOOL_DESCRIPTIONS: str = """
可用工具（每个返回 {"success": bool, "message": str, "data": dict}）：

1. query_order(order_id: str) — 查订单状态
2. query_logistics(keyword: str) — 查物流轨迹，支持订单号或运单号
3. update_address(order_id: str, new_address: str) — 改收货地址（已签收/已取消会拒绝）
4. refund_price_diff(order_id: str, amount: float) — 退差价（金额>500会拒绝）
5. update_remark(order_id: str, remark: str) — 加备注
6. urge_delivery(tracking_no: str) — 催派送（已签收会拒绝）
7. process_refund(order_id: str, amount: float) — 退款（金额>1000、已取消会拒绝）
8. process_exchange(order_id: str) — 换货（已取消会拒绝）
"""


# ── 风险信号检测（安全边界，确定性规则）────────────────────────

from datetime import datetime, timedelta

_RISK_KEYWORDS = [
    "12315", "315", "投诉", "赔偿", "假货", "翻新", "骗子", "诈骗",
    "气死", "太垃圾", "曝光", "等着吧", "报警", "法院", "律师",
]

# 物流异常 + 情绪词 → 转人工
_LOGISTICS_EMOTION_KEYWORDS = [
    "丢了吧", "不能耽误", "急", "赶紧", "说法", "投诉",
    "丢了", "赔", "损失", "不行", "急死",
]

# 物流停滞：运输中或异常 + 超此天数 → 转人工（不依赖情绪词）
_LOGISTICS_STALE_DAYS = 3


def _has_emotion(msg: str) -> bool:
    """检查消息是否包含物流相关情绪词。"""
    return any(kw in msg for kw in _LOGISTICS_EMOTION_KEYWORDS)


def _check_fraud(msg: str) -> str | None:
    """检测防诈骗信号，匹配到则返回描述，否则 None。"""
    fraud_patterns = [
        ("接到电话", "快递丢"),
        ("收到短信", "快递丢"),
        ("接到电话", "核实信息"),
        ("收到短信", "核实信息"),
        ("电话说", "丢"),
        ("短信说", "丢"),
        ("打电话", "丢件"),
        ("打电话", "快递丢了"),
    ]
    for a, b in fraud_patterns:
        if a in msg and b in msg:
            return f"用户可能遭遇快递理赔诈骗（匹配「{a}」+「{b}」），需人工介入并提醒用户谨防诈骗"
    return None


def _check_logistics_stale(tool_results: list[dict]) -> str | None:
    """检测物流停滞：运输中或异常 + 超过 N 天未更新。

    纯时间判断，不依赖用户情绪词。数据层已精确对齐时间戳，
    只有真正长期未更新的记录才会触发。
    """
    threshold = datetime.now() - timedelta(days=_LOGISTICS_STALE_DAYS)
    for r in tool_results:
        data = r.get("data", {})
        if not isinstance(data, dict):
            continue
        status = data.get("status", "")
        if status not in ("运输中", "异常"):
            continue
        updated_str = data.get("updated_at", "")
        if not updated_str:
            continue
        try:
            updated_dt = datetime.strptime(updated_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if updated_dt < threshold:
            days_stale = (datetime.now() - updated_dt).days
            return (
                f"物流停滞：运单 {data.get('tracking_no', '?')}（{status}），"
                f"最后更新 {updated_str}（已 {days_stale} 天未更新），需人工核实"
            )
    return None


def _detect_risks(state: TicketState) -> list[str]:
    """检测风险信号，返回触发的风险原因列表。每个原因一行中文描述。"""
    risks: list[str] = []
    msg = state.get("user_message", "")
    tool_results = state.get("tool_results", [])

    # 1. 敏感词检测（最高优先）
    for kw in _RISK_KEYWORDS:
        if kw in msg:
            risks.append(f"用户消息含敏感词「{kw}」，需人工核实处理")
            break

    # 2. 防诈骗信号检测
    fraud_reason = _check_fraud(msg)
    if fraud_reason:
        risks.append(fraud_reason)

    # 3. 物流停滞检测（运输中/异常 + 超过 3 天，纯时间判断）
    stale_reason = _check_logistics_stale(tool_results)
    if stale_reason:
        risks.append(stale_reason)

    # 4. 物流异常 + 情绪组合检测（有明确情绪/投诉信号才转）
    if _has_emotion(msg):
        for r in tool_results:
            data = r.get("data", {})
            if isinstance(data, dict) and data.get("status") == "异常":
                risks.append(
                    f"物流状态异常（运单 {data.get('tracking_no', '?')}）且用户有情绪信号，需人工跟进"
                )
                break

    # 5. 已取消订单 + 用户要求退款
    order_cancelled = False
    for r in tool_results:
        data = r.get("data", {})
        if isinstance(data, dict) and data.get("order_status") == "已取消":
            order_cancelled = True
            break
        if isinstance(data, dict) and data.get("status") == "已取消":
            order_cancelled = True
            break

    if order_cancelled:
        refund_words = ["退款", "退钱", "退", "返还", "退回", "还我"]
        if any(w in msg for w in refund_words):
            risks.append("订单已取消但用户要求退款，需人工核实是否已退款")

    return risks


# ── 节点函数 ────────────────────────────────────────────────────

def classify_node(state: TicketState) -> dict[str, Any]:
    """意图分类 + 关键信息提取。

    用 json_object 模式让 LLM 返回固定格式 JSON，解析后写入 category 和 extracted_info。
    """
    t0 = time.time()
    history_text = ""
    if state.get("history"):
        items = state["history"]
        history_text = "该用户历史工单：\n" + "\n".join(
            f"- [{h.get('category','')}] {h.get('resolution','')} ({h.get('created_at','')})"
            for h in items
        )

    system_prompt = (
        "你是客服工单分类助手。根据用户消息判断意图并提取关键信息，输出 JSON。\n"
        "类别只能是：订单、物流、售后。\n"
        "订单类：查订单状态、改地址、改备注、退差价\n"
        "物流类：查物流、催派送\n"
        "售后类：退款、换货\n"
        "提取规则：\n"
        "- order_id: ORD- 开头的订单号，没有则为 null\n"
        "- tracking_no: 大写字母+数字的运单号（10-15位），没有则为 null\n"
        "- amount: 消息中提到的金额数字，没有则为 null\n"
        f"{history_text}"
    )

    user_prompt = (
        f"用户消息：{state['user_message']}\n\n"
        "请返回一个 JSON 对象（不要包含在代码块中），格式：\n"
        '{"category": "订单|物流|售后", "order_id": "ORD-xxx 或 null", '
        '"tracking_no": "运单号 或 null", "amount": 数字 或 null}'
    )

    llm = _get_llm()
    reply = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)],
        response_format={"type": "json_object"},
    )
    raw = reply.content if hasattr(reply, "content") else str(reply)

    try:
        parsed = _parse_json_from_response(str(raw))
    except json.JSONDecodeError:
        # 解析失败降级：返回空值，由后续节点转人工
        return {
            "category": "售后",
            "extracted_info": {"order_id": None, "tracking_no": None, "amount": None},
            **_timing(state, "classify", t0),
        }

    return {
        "category": parsed.get("category", "售后"),
        "extracted_info": {
            "order_id": parsed.get("order_id"),
            "tracking_no": parsed.get("tracking_no"),
            "amount": parsed.get("amount"),
        },
        **_timing(state, "classify", t0),
    }


def handle_node(state: TicketState) -> dict[str, Any]:
    """工具选择与执行。

    LLM 根据分类和提取信息选择需调用的工具，逐一执行，结果（含失败）写入 tool_results。
    """
    t0 = time.time()
    category = state.get("category", "")
    extracted = state.get("extracted_info", {})
    user_message = state.get("user_message", "")

    prompt = (
        f"用户消息：{user_message}\n"
        f"分类结果：{category}\n"
        f"已提取信息：{json.dumps(extracted, ensure_ascii=False)}\n\n"
        f"{_TOOL_DESCRIPTIONS}\n\n"
        "请决定需要调用哪些工具，返回一个 JSON 对象（不要包含在代码块中），格式：\n"
        '{"tools": [{"tool_name": "工具名", "arguments": {参数键值对}}]}\n\n'
        "规则（按顺序执行）：\n"
        "- **必须遵守（关键规则）**：执行 process_refund / process_exchange / refund_price_diff / update_address 之前，"
        "必须先调用 query_order 验证订单。例如退款场景，tools 数组必须为 "
        '[{"tool_name":"query_order","arguments":{...}},{"tool_name":"process_refund","arguments":{...}}]。'
        " 违反此规则会导致工具执行失败。\n"
        "- 只选与分类和提取信息直接相关的工具\n"
        "- new_address、remark 用用户原话中的文本\n"
        "- amount 用提取到的数字，没有则填 0\n"
        "- 没有订单号也没有运单号时返回空列表\n"
        "- 不要选无关工具"
    )

    llm = _get_llm()
    reply = llm.invoke(
        [HumanMessage(content=prompt)],
        response_format={"type": "json_object"},
    )
    raw = reply.content if hasattr(reply, "content") else str(reply)

    try:
        selection = _parse_json_from_response(str(raw))
    except json.JSONDecodeError:
        return {"tool_results": [], **_timing(state, "handle", t0)}

    tools_to_call: list[dict] = selection.get("tools", [])

    tool_results: list[dict[str, Any]] = []
    for item in tools_to_call:
        tool_name = item.get("tool_name", "")
        arguments = item.get("arguments", {})
        func = _TOOL_MAP.get(tool_name)

        if func is None:
            tool_results.append({
                "tool_name": tool_name,
                "success": False,
                "message": f"未知工具 {tool_name}",
                "data": {},
            })
            continue

        try:
            result = func(**arguments)
        except Exception as e:
            result = {
                "success": False,
                "message": f"工具调用异常：{str(e)}",
                "data": {},
            }

        result["tool_name"] = tool_name
        result["arguments"] = arguments
        tool_results.append(result)

    return {"tool_results": tool_results, **_timing(state, "handle", t0)}


def decide_node(state: TicketState) -> dict[str, Any]:
    """决策节点——风险检测 + 工具结果判断，纯规则不调 LLM。

    优先级：
    1. 风险信号命中 → 强制 escalate
    2. 工具全部 success=true → auto
    3. 任一工具失败或未调用 → escalate
    """
    t0 = time.time()
    tool_results = state.get("tool_results", [])

    # 最高优先：风险信号检测
    risks = _detect_risks(state)
    if risks:
        return {
            "status": "已关闭",
            "escalate_reason": "风险拦截——" + "；".join(risks),
            "resolution": "",
            **_timing(state, "decide", t0),
        }

    # 次优先：工具执行结果判断
    if not tool_results:
        return {
            "status": "已关闭",
            "escalate_reason": "无法确定需要调用的工具，信息不足，转人工处理。",
            "resolution": "",
            **_timing(state, "decide", t0),
        }

    failures = [r for r in tool_results if not r.get("success", False)]
    if failures:
        reasons = "; ".join(
            f"{r.get('tool_name','')}: {r.get('message','')}" for r in failures
        )
        return {
            "status": "已关闭",
            "escalate_reason": f"工具执行失败——{reasons}",
            "resolution": "",
            **_timing(state, "decide", t0),
        }

    return {"status": "处理中", "escalate_reason": "", **_timing(state, "decide", t0)}


def reply_auto_node(state: TicketState) -> dict[str, Any]:
    """生成自动回复——LLM 根据工具结果合成对用户的友好回复。"""
    t0 = time.time()
    tool_results = state.get("tool_results", [])
    user_message = state.get("user_message", "")

    results_text = json.dumps(tool_results, ensure_ascii=False, indent=2)
    prompt = (
        f"用户消息：{user_message}\n\n"
        f"系统处理结果：\n{results_text}\n\n"
        "请生成给用户的回复。要求：语气亲切、简洁直接、包含关键信息（状态/金额/时间），300 字以内。"
    )

    llm = _get_llm()
    reply = llm.invoke([HumanMessage(content=prompt)])
    resolution = reply.content if hasattr(reply, "content") else str(reply)

    return {"status": "已解决", "resolution": str(resolution), **_timing(state, "reply_auto", t0)}


def escalate_node(state: TicketState) -> dict[str, Any]:
    """生成转人工说明——汇总已有信息方便人工快速接手。"""
    t0 = time.time()
    extracted = state.get("extracted_info", {})
    tool_results = state.get("tool_results", [])
    escalate_reason = state.get("escalate_reason", "")
    category = state.get("category", "")

    lines = [
        f"【转人工】分类：{category}",
        f"原因：{escalate_reason}",
        f"已提取信息：{json.dumps(extracted, ensure_ascii=False)}",
    ]
    if tool_results:
        lines.append(f"工具执行记录：{json.dumps(tool_results, ensure_ascii=False, indent=2)}")

    return {"status": "已关闭", "resolution": "\n".join(lines), **_timing(state, "escalate", t0)}
