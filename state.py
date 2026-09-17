"""
工单状态定义 — 基于 TypedDict 的客服工单状态模型。
整个 LangGraph 图的所有节点共享此状态，按字段读写。
"""

from typing import TypedDict, Any


class TicketState(TypedDict, total=False):
    """客服工单状态，贯穿分类→提取→执行→判断的全流程。

    字段说明：
    - ticket_id: 工单唯一标识
    - user_message: 用户原始消息文本
    - category: 意图分类结果，"订单" / "物流" / "售后"
    - extracted_info: 提取的结构化关键信息
    - tool_results: 工具调用结果列表
    - resolution: 最终处理结果描述
    - status: 工单当前状态
    - escalate_reason: 转人工原因
    - history: 该用户历史工单摘要列表
    - trace_id: 跨服务关联标识（一次用户请求在编排器/知识库/客服三处共用）
    - _timings: 各节点耗时统计（ms），由 logger 自动填充
    """

    ticket_id: str
    user_message: str
    category: str
    extracted_info: dict[str, Any]
    tool_results: list[dict[str, Any]]
    resolution: str
    status: str
    escalate_reason: str
    user_identifier: str  # 用户标识（手机号等），用于关联历史
    history: list[str]
    trace_id: str  # 跨服务关联标识（由 HTTP 入口注入）
    risk_blocked: bool  # P2：写操作是否被风险门禁拦下（未执行）
    risk_reasons: list[str]  # P2：命中的风险原因列表
    proposed_action: dict[str, Any]  # P2：待人工审批的确定性动作（动作+快照+规则版本）
    _timings: dict[str, int]  # 节点名 → 毫秒
