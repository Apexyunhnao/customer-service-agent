"""
工具函数 — Agent 可调用的 8 个业务工具，以 SQLite 为后端。
每个工具返回统一格式：{success: bool, message: str, data: dict}
业务规则写死在函数体内，作为 Agent 的安全边界；LLM 无法绕过。
"""

import sqlite3
from pathlib import Path
from typing import Any

# ── 数据库连接 ──────────────────────────────────────────────────

_DB_PATH: Path = Path(__file__).parent / "data" / "business.db"


def _get_conn() -> sqlite3.Connection:
    """返回业务数据库连接，首次调用时自动建辅助表。"""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row  # 支持按列名访问
    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_remarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            remark TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        )
    """)
    conn.commit()
    return conn


def _safe_str(val: Any) -> str:
    """安全转字符串，None → 空串。"""
    return str(val) if val is not None else ""


# ── 工具函数 ────────────────────────────────────────────────────

def query_order(order_id: str) -> dict[str, Any]:
    """查询订单状态。

    Args:
        order_id: 订单号，如 "ORD-1003"

    Returns:
        {success, message, data: {order_id, status, product, amount, created_at, customer}}
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT o.*, c.name AS customer_name, c.phone AS customer_phone, "
            "c.address AS customer_address, c.vip AS customer_vip "
            "FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE o.order_id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，请核实单号后重试。",
            "data": {},
        }

    return {
        "success": True,
        "message": f"订单 {order_id} 当前状态为「{row['status']}」。",
        "data": {
            "order_id": row["order_id"],
            "status": row["status"],
            "product": row["product"],
            "amount": row["amount"],
            "created_at": row["created_at"],
            "customer": {
                "name": _safe_str(row["customer_name"]),
                "phone": _safe_str(row["customer_phone"]),
                "address": _safe_str(row["customer_address"]),
                "vip": _safe_str(row["customer_vip"]),
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
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM logistics WHERE tracking_no = ? OR order_id = ?",
            (keyword, keyword),
        ).fetchone()

    if row is None:
        return {
            "success": False,
            "message": f"未找到 {keyword} 对应的物流信息，请检查单号。",
            "data": {},
        }

    return {
        "success": True,
        "message": f"运单 {row['tracking_no']}（{row['company']}）当前状态：{row['status']}，位置：{row['current_location']}。",
        "data": {
            "company": row["company"],
            "tracking_no": row["tracking_no"],
            "current_location": row["current_location"],
            "status": row["status"],
            "order_id": row["order_id"],
            "updated_at": row["updated_at"] or "",
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
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT o.order_id, o.status, o.customer_id, c.address "
            "FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE o.order_id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法修改地址。",
            "data": {},
        }

    if row["status"] == "已签收":
        return {
            "success": False,
            "message": f"订单 {order_id} 已签收，无法修改收货地址。",
            "data": {"order_status": row["status"]},
        }

    if row["status"] == "已取消":
        return {
            "success": False,
            "message": f"订单 {order_id} 已取消，无法修改收货地址。",
            "data": {"order_status": row["status"]},
        }

    old_address = _safe_str(row["address"])
    customer_id = row["customer_id"]

    # 真实写入数据库
    with _get_conn() as write_conn:
        write_conn.execute(
            "UPDATE customers SET address = ? WHERE id = ?",
            (new_address, customer_id),
        )
        write_conn.commit()

    return {
        "success": True,
        "message": f"订单 {order_id} 收货地址已从「{old_address}」修改为「{new_address}」。",
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
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT order_id, status, amount FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法退差价。",
            "data": {},
        }

    if amount > 500:
        return {
            "success": False,
            "message": f"退差价金额 {amount} 元超出自动处理限额（最高 500 元），需转人工审批。",
            "data": {"refund_amount": amount, "limit": 500},
        }

    if row["status"] == "已取消":
        return {
            "success": False,
            "message": f"订单 {order_id} 已取消，无法退差价。",
            "data": {"order_status": row["status"]},
        }

    return {
        "success": True,
        "message": f"订单 {order_id} 差价 {amount} 元已退还至原支付账户，预计 1-3 个工作日到账。",
        "data": {
            "refund_amount": amount,
            "order_amount": row["amount"],
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
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT order_id FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法添加备注。",
            "data": {},
        }

    # 真实写入 order_remarks 表
    with _get_conn() as write_conn:
        write_conn.execute(
            "INSERT INTO order_remarks (order_id, remark) VALUES (?, ?)",
            (order_id, remark),
        )
        write_conn.commit()

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
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT tracking_no, company, status FROM logistics WHERE tracking_no = ?",
            (tracking_no,),
        ).fetchone()

    if row is None:
        return {
            "success": False,
            "message": f"运单 {tracking_no} 不存在，请核实单号。",
            "data": {},
        }

    if row["status"] == "已签收":
        return {
            "success": False,
            "message": f"运单 {tracking_no} 已签收，无需催派送。",
            "data": {"status": row["status"]},
        }

    return {
        "success": True,
        "message": f"已向 {row['company']} 发送催派送通知，运单 {tracking_no} 将优先处理。",
        "data": {
            "tracking_no": tracking_no,
            "company": row["company"],
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
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT order_id, status, amount FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法退款。",
            "data": {},
        }

    if row["status"] == "已取消":
        return {
            "success": False,
            "message": f"订单 {order_id} 已取消，无需重复退款。",
            "data": {"order_status": row["status"]},
        }

    if amount > 1000:
        return {
            "success": False,
            "message": f"退款金额 {amount} 元超出自动处理限额（最高 1000 元），需转人工审批。",
            "data": {"refund_amount": amount, "limit": 1000},
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
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT order_id, status, product FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        return {
            "success": False,
            "message": f"订单 {order_id} 不存在，无法申请换货。",
            "data": {},
        }

    if row["status"] == "已取消":
        return {
            "success": False,
            "message": f"订单 {order_id} 已取消，无法申请换货。",
            "data": {"order_status": row["status"]},
        }

    return {
        "success": True,
        "message": f"订单 {order_id}（{row['product']}）换货申请已提交，新商品将在 1-3 个工作日发出。",
        "data": {
            "order_id": order_id,
            "product": row["product"],
        },
    }
