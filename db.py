"""
SQLite 存储层 — 工单持久化与历史查询。
数据库文件：data/tickets.db，首次调用自动建表。
"""

import sqlite3
import os
from datetime import datetime
from pathlib import Path

DB_PATH: Path = Path(__file__).parent / "data" / "tickets.db"


def _ensure_dir() -> None:
    """确保 data 目录存在。"""
    os.makedirs(DB_PATH.parent, exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，自动建表。"""
    _ensure_dir()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT UNIQUE NOT NULL,
            user_identifier TEXT DEFAULT '',
            user_message TEXT NOT NULL,
            category TEXT DEFAULT '',
            status TEXT DEFAULT '新建',
            resolution TEXT DEFAULT '',
            escalate_reason TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def create_ticket(
    ticket_id: str,
    user_message: str,
    user_identifier: str = "",
    category: str = "",
    status: str = "新建",
    resolution: str = "",
    escalate_reason: str = "",
) -> dict:
    """保存一条新工单到数据库。

    Args:
        ticket_id: 工单唯一标识
        user_message: 用户原始消息
        user_identifier: 用户标识（手机号或姓名），用于关联历史
        category: 分类结果
        status: 工单状态
        resolution: 处理结果
        escalate_reason: 转人工原因

    Returns:
        {success, ticket_id}
    """
    conn = _get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            """INSERT INTO tickets
               (ticket_id, user_identifier, user_message, category, status,
                resolution, escalate_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticket_id, user_identifier, user_message, category, status,
             resolution, escalate_reason, now),
        )
        conn.commit()
        return {"success": True, "ticket_id": ticket_id}
    except sqlite3.IntegrityError:
        return {"success": False, "ticket_id": ticket_id, "reason": "工单ID重复"}
    finally:
        conn.close()


def update_ticket_status(
    ticket_id: str,
    status: str,
    resolution: str = "",
) -> dict:
    """更新工单状态和处理结果。

    Args:
        ticket_id: 工单标识
        status: 新状态
        resolution: 处理结果描述

    Returns:
        {success, ticket_id}
    """
    conn = _get_conn()
    conn.execute(
        "UPDATE tickets SET status = ?, resolution = ? WHERE ticket_id = ?",
        (status, resolution, ticket_id),
    )
    conn.commit()
    conn.close()
    return {"success": True, "ticket_id": ticket_id, "status": status}


def get_user_history(user_identifier: str, limit: int = 5) -> list[dict]:
    """查询用户的历史工单摘要，按时间倒序返回最近 N 条。

    Args:
        user_identifier: 用户标识（手机号或姓名），支持模糊匹配
        limit: 返回条数上限，默认 5

    Returns:
        历史工单列表，每项 {ticket_id, category, status, resolution, created_at}
    """
    if not user_identifier:
        return []

    conn = _get_conn()
    rows = conn.execute(
        """SELECT ticket_id, category, status, resolution, created_at
           FROM tickets
           WHERE user_identifier LIKE ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (f"%{user_identifier}%", limit),
    ).fetchall()
    conn.close()

    return [dict(r) for r in rows]
