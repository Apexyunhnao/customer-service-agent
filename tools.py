"""
工具函数 — Agent 可调用的 8 个业务工具，全部以 mock 数据为后端。
每个工具返回统一格式：{success: bool, message: str, data: dict}
业务规则写死在函数体内，作为 Agent 的安全边界；LLM 无法绕过。
"""

import json
from pathlib import Path
from typing import Any

# ── 加载 mock 数据 ──────────────────────────────────────────────

_DB_PATH: Path = Path(__file__).parent / "data" / "mock_db.json"


def _load_db() -> dict[str, Any]:
    """读取 mock 数据库文件，返回 dict。"""
    with open(_DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_order(db: dict, order_id: str) -> dict | None:
    """在 orders 列表中按 order_id 查找，找不到返回 None。"""
    for o in db["orders"]:
        if o["order_id"] == order_id:
            return o
    return None


def _find_logistics(db: dict, keyword: str) -> dict | None:
    """按 tracking_no 或 order_id 查找物流记录。"""
    for l in db["logistics"]:
        if l["tracking_no"] == keyword or l["order_id"] == keyword:
            return l
    return None


def _find_customer(db: dict, customer_id: int) -> dict | None:
    """按 customer_id 查找客户。"""
    for c in db["customers"]:
        if c["id"] == customer_id:
            return c
    return None


# ── 工具函数 ────────────────────────────────────────────────────

def query_order(order_id: str) -> dict[str, Any]:
    """查询订单状态。

    Args:
        order_id: 订单号，如 "ORD-1003"

    Returns:
        {success, message, data: {order_id, status, product, amount, created_at, customer}}
    """
    db = _load_db()
    order = _find_order(db, order_id)

    if order is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，请核实单号后重试。",
            "data": {},
        }

    customer = _find_customer(db, order["customer_id"])
    return {
        "success": True,
        "message": f"订单 {order_id} 当前状态为「{order['status']}」。",
        "data": {
            "order_id": order["order_id"],
            "status": order["status"],
            "product": order["product"],
            "amount": order["amount"],
            "created_at": order["created_at"],
            "customer": {
                "name": customer["name"] if customer else "未知",
                "phone": customer["phone"] if customer else "",
                "address": customer["address"] if customer else "",
                "vip": customer["vip"] if customer else "",
            },
        },
    }


def query_logistics(keyword: str) -> dict[str, Any]:
    """查询物流轨迹，支持订单号或运单号。

    Args:
        keyword: 订单号（如 "ORD-1002"）或运单号（如 "JD9876543210"）

    Returns:
        {success, message, data: {company, tracking_no, current_location, status, order_id}}
    """
    db = _load_db()
    logistics = _find_logistics(db, keyword)

    if logistics is None:
        return {
            "success": False,
            "message": f"未找到 {keyword} 对应的物流信息，请检查单号。",
            "data": {},
        }

    return {
        "success": True,
        "message": f"运单 {logistics['tracking_no']}（{logistics['company']}）当前状态：{logistics['status']}，位置：{logistics['current_location']}。",
        "data": {
            "company": logistics["company"],
            "tracking_no": logistics["tracking_no"],
            "current_location": logistics["current_location"],
            "status": logistics["status"],
            "order_id": logistics["order_id"],
            "updated_at": logistics.get("updated_at", ""),
        },
    }


def update_address(order_id: str, new_address: str) -> dict[str, Any]:
    """修改订单收货地址。

    安全边界：已签收、已取消的订单不可修改地址。

    Args:
        order_id: 订单号
        new_address: 新收货地址全文

    Returns:
        {success, message, data: {old_address, new_address}}
    """
    db = _load_db()
    order = _find_order(db, order_id)

    if order is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法修改地址。",
            "data": {},
        }

    if order["status"] == "已签收":
        return {
            "success": False,
            "message": f"订单 {order_id} 已签收，无法修改收货地址。",
            "data": {"order_status": order["status"]},
        }

    if order["status"] == "已取消":
        return {
            "success": False,
            "message": f"订单 {order_id} 已取消，无法修改收货地址。",
            "data": {"order_status": order["status"]},
        }

    customer = _find_customer(db, order["customer_id"])
    old_address = customer["address"] if customer else "未知"

    return {
        "success": True,
        "message": f"订单 {order_id} 收货地址已修改为「{new_address}」。",
        "data": {
            "old_address": old_address,
            "new_address": new_address,
        },
    }


def refund_price_diff(order_id: str, amount: float) -> dict[str, Any]:
    """退差价——商品降价后为已购用户退还差额。

    安全边界：单笔退款金额超过 500 元拒绝，需转人工审批。

    Args:
        order_id: 订单号
        amount: 申请退还的差价金额（元）

    Returns:
        {success, message, data: {refund_amount, order_amount}}
    """
    db = _load_db()
    order = _find_order(db, order_id)

    if order is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法退差价。",
            "data": {},
        }

    if amount > 500:
        return {
            "success": False,
            "message": f"退差价金额 {amount} 元超出自动处理限额（最高 500 元），需转人工审批。",
            "data": {
                "refund_amount": amount,
                "limit": 500,
            },
        }

    if order["status"] == "已取消":
        return {
            "success": False,
            "message": f"订单 {order_id} 已取消，无法退差价。",
            "data": {"order_status": order["status"]},
        }

    return {
        "success": True,
        "message": f"订单 {order_id} 差价 {amount} 元已退还至原支付账户，预计 1-3 个工作日到账。",
        "data": {
            "refund_amount": amount,
            "order_amount": order["amount"],
        },
    }


def update_remark(order_id: str, remark: str) -> dict[str, Any]:
    """给订单添加备注。

    Args:
        order_id: 订单号
        remark: 备注内容

    Returns:
        {success, message, data: {remark}}
    """
    db = _load_db()
    order = _find_order(db, order_id)

    if order is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法添加备注。",
            "data": {},
        }

    return {
        "success": True,
        "message": f"已为订单 {order_id} 添加备注：「{remark}」。",
        "data": {
            "order_id": order_id,
            "remark": remark,
        },
    }


def urge_delivery(tracking_no: str) -> dict[str, Any]:
    """催派送——通知物流方加快配送。

    Args:
        tracking_no: 运单号

    Returns:
        {success, message, data: {tracking_no, company}}
    """
    db = _load_db()
    logistics = _find_logistics(db, tracking_no)

    if logistics is None:
        return {
            "success": False,
            "message": f"运单 {tracking_no} 不存在，请核实单号。",
            "data": {},
        }

    if logistics["status"] == "已签收":
        return {
            "success": False,
            "message": f"运单 {tracking_no} 已签收，无需催派送。",
            "data": {"status": logistics["status"]},
        }

    return {
        "success": True,
        "message": f"已向 {logistics['company']} 发送催派送通知，运单 {tracking_no} 将优先处理。",
        "data": {
            "tracking_no": tracking_no,
            "company": logistics["company"],
        },
    }


def process_refund(order_id: str, amount: float) -> dict[str, Any]:
    """处理退款申请。

    安全边界：单笔退款金额超过 1000 元拒绝，已取消订单拒绝。

    Args:
        order_id: 订单号
        amount: 退款金额（元）

    Returns:
        {success, message, data: {refund_amount}}
    """
    db = _load_db()
    order = _find_order(db, order_id)

    if order is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法退款。",
            "data": {},
        }

    if order["status"] == "已取消":
        return {
            "success": False,
            "message": f"订单 {order_id} 已取消，无需重复退款。",
            "data": {"order_status": order["status"]},
        }

    if amount > 1000:
        return {
            "success": False,
            "message": f"退款金额 {amount} 元超出自动处理限额（最高 1000 元），需转人工审批。",
            "data": {
                "refund_amount": amount,
                "limit": 1000,
            },
        }

    return {
        "success": True,
        "message": f"订单 {order_id} 退款 {amount} 元已受理，预计 3-5 个工作日原路退回。",
        "data": {
            "order_id": order_id,
            "refund_amount": amount,
        },
    }


def process_exchange(order_id: str) -> dict[str, Any]:
    """处理换货申请。

    安全边界：已取消订单拒绝换货。

    Args:
        order_id: 订单号

    Returns:
        {success, message, data: {order_id, product}}
    """
    db = _load_db()
    order = _find_order(db, order_id)

    if order is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法申请换货。",
            "data": {},
        }

    if order["status"] == "已取消":
        return {
            "success": False,
            "message": f"订单 {order_id} 已取消，无法申请换货。",
            "data": {"order_status": order["status"]},
        }

    return {
        "success": True,
        "message": f"订单 {order_id}（{order['product']}）换货申请已提交，新商品将在 1-3 个工作日发出。",
        "data": {
            "order_id": order_id,
            "product": order["product"],
        },
    }
