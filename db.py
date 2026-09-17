"""
SQLite 存储层 — 工单持久化与历史查询。
数据库文件：data/tickets.db，首次调用自动建表。
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH: Path = Path(__file__).parent / "data" / "tickets.db"


def _ensure_dir() -> None:
    """确保 data 目录存在。"""
    os.makedirs(DB_PATH.parent, exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，自动建表 + 幂等迁移。"""
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
    # 轻量迁移：老库补 steps 列（存完整执行链路，供观测页回放）
    # 注意 CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，必须显式迁移
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tickets)")]
    if "steps" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN steps TEXT NOT NULL DEFAULT '[]'")
    # 轻量迁移：补 trace_id 列（跨服务关联标识，一次用户请求在三个服务里共用一个）
    if "trace_id" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''")
    # 轻量迁移（P2）：补 proposed_action 列 —— 高风险工单"待审批的确定性动作"（JSON）。
    # 批准时执行的是这条结构化动作，而不是重放历史的自由文本工具调用。
    if "proposed_action" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN proposed_action TEXT NOT NULL DEFAULT ''")
    if "risk_reasons" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN risk_reasons TEXT NOT NULL DEFAULT '[]'")
    # 审计表（只追加，不修改）：记录每一次状态变化的操作者、时间、前后状态与原因
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticket_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            actor_role TEXT NOT NULL DEFAULT '',
            from_status TEXT NOT NULL DEFAULT '',
            to_status TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT 'success',
            source_trace_id TEXT NOT NULL DEFAULT '',
            approval_trace_id TEXT NOT NULL DEFAULT '',
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
    steps: list | None = None,
    trace_id: str = "",
    proposed_action: dict | None = None,
    risk_reasons: list | None = None,
) -> dict:
    """保存一条新工单到数据库（含执行链路）。

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
                resolution, escalate_reason, created_at, steps, trace_id,
                proposed_action, risk_reasons)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticket_id, user_identifier, user_message, category, status,
             resolution, escalate_reason, now,
             json.dumps(steps or [], ensure_ascii=False), trace_id,
             json.dumps(proposed_action or {}, ensure_ascii=False),
             json.dumps(risk_reasons or [], ensure_ascii=False)),
        )
        conn.commit()
        # 审计：工单创建本身就记录一条（含它一落地就是什么状态 —— 自动解决 / 待审核 / 处理失败）
        audit_log(conn, ticket_id=ticket_id, action="created",
                  actor_id="system", actor_role="agent-pipeline",
                  from_status="", to_status=status,
                  reason=escalate_reason or "",
                  outcome="success" if status != STATE_FAILED else "failed",
                  source_trace_id=trace_id)
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
        user_identifier: 用户标识（手机号或姓名），长度需 ≥4 才查询（避免短串模糊匹配捞出一堆）
        limit: 返回条数上限，默认 5

    Returns:
        历史工单列表，每项 {ticket_id, category, status, resolution, created_at}
    """
    if not user_identifier or len(user_identifier) < 4:
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


def list_recent_tickets(limit: int = 30) -> list[dict]:
    """最近 N 条工单（含执行链路、风险信号与待审批动作），供观测页使用。"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, ticket_id, user_identifier, user_message, category, status,
                  resolution, escalate_reason, created_at, steps, trace_id,
                  proposed_action, risk_reasons
           FROM tickets ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()

    out: list[dict] = []
    for r in rows:
        item = dict(r)
        for col, empty in (("steps", []), ("risk_reasons", []), ("proposed_action", {})):
            try:
                item[col] = json.loads(item.get(col) or json.dumps(empty))
            except (json.JSONDecodeError, TypeError):
                item[col] = empty
        out.append(item)
    return out


def ticket_stats() -> dict:
    """工单统计：总数 / 分类分布 / 转人工数，供观测页指标卡使用。"""
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    by_cat = {
        (r["category"] or "未分类"): r["n"]
        for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM tickets GROUP BY category"
        ).fetchall()
    }
    escalated = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE escalate_reason != ''"
    ).fetchone()[0]
    auto = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE escalate_reason = ''"
    ).fetchone()[0]
    conn.close()
    return {"total": total, "by_category": by_cat, "escalated": escalated, "auto": auto}


# ── 状态机（P2 人工审核闭环） ─────────────────────────────
#
#  新建 → 处理中 → ┬─ 已解决（自动处理成功）
#                  ├─ 待审核 → ┬─ 已解决（客服批准）
#                  │            └─ 已驳回（客服驳回）
#                  └─ 处理失败（工具失败等：不能靠"批准"治好，必须走重试/人工补充）
#
# 转换规则只在服务端裁决；前端按钮只是便利，不是安全边界。

STATE_NEW = "新建"
STATE_PROCESSING = "处理中"
STATE_PENDING_REVIEW = "待审核"
STATE_RESOLVED = "已解决"
STATE_REJECTED = "已驳回"
STATE_FAILED = "处理失败"

ALL_TICKET_STATES = [STATE_NEW, STATE_PROCESSING, STATE_PENDING_REVIEW,
                     STATE_RESOLVED, STATE_REJECTED, STATE_FAILED]

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    STATE_NEW: {STATE_PROCESSING},
    STATE_PROCESSING: {STATE_RESOLVED, STATE_PENDING_REVIEW, STATE_FAILED},
    STATE_PENDING_REVIEW: {STATE_RESOLVED, STATE_REJECTED},
    STATE_RESOLVED: set(),
    STATE_REJECTED: set(),
    STATE_FAILED: set(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    """状态转换是否合法。"""
    return to_status in ALLOWED_TRANSITIONS.get(from_status, set())


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def audit_log(conn: sqlite3.Connection, *, ticket_id: str, action: str,
              actor_id: str = "", actor_role: str = "", from_status: str = "",
              to_status: str = "", reason: str = "", outcome: str = "success",
              source_trace_id: str = "", approval_trace_id: str = "") -> None:
    """写一条审计记录（只追加，调用方负责提交事务）。

    `actor_id`/`actor_role` 存不可变的身份标识而非展示名；`source_trace_id` 是 Agent 运行时
    的 trace，`approval_trace_id` 是审批 HTTP 请求的 trace —— 两者是不同请求，不能混用。
    """
    conn.execute(
        """INSERT INTO ticket_audit
           (event_id, ticket_id, action, actor_id, actor_role, from_status, to_status,
            reason, outcome, source_trace_id, approval_trace_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uuid.uuid4().hex[:16], ticket_id, action, actor_id, actor_role, from_status,
         to_status, (reason or "")[:500], outcome, source_trace_id, approval_trace_id, _now()),
    )


def review_ticket(ticket_id: str, *, approve: bool, actor_id: str, actor_role: str,
                  note: str = "", approval_trace_id: str = "", executor=None) -> dict:
    """审批待审核工单：**原子条件更新 + 同事务写审计**。

    并发安全的关键（比 Idempotency-Key 更根本）：
    1. `BEGIN IMMEDIATE` 先拿写锁；
    2. `UPDATE ... WHERE ticket_id = ? AND status = '待审核'` 后检查 rowcount ——
       两个并发审批只有一个能改成功，另一个拿到 0 行 → 判为冲突、不重复执行。
    "先查状态再更新"会被并发穿透，这是幂等键挡不住的那一类问题。

    Args:
        approve: True=批准 → 已解决；False=驳回 → 已驳回
        executor: 可选回调，入参 proposed_action(dict)，返回 {"success":bool,"message":str}。
                  传入即在**同一事务内**执行那条确定性动作；执行失败则整单转「处理失败」。
    """
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)  # 手动控制事务
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, resolution, proposed_action, trace_id FROM tickets WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"success": False, "code": "NOT_FOUND", "message": "工单不存在"}

        cur_status = row["status"]
        if cur_status != STATE_PENDING_REVIEW:
            conn.rollback()
            return {"success": False, "code": "INVALID_STATE",
                    "message": f"工单当前状态为「{cur_status}」，只有「{STATE_PENDING_REVIEW}」可审批"}

        proposed: dict = {}
        if row["proposed_action"]:
            try:
                proposed = json.loads(row["proposed_action"])
            except (json.JSONDecodeError, TypeError):
                proposed = {}

        resolution = row["resolution"] or ""

        # 批准：在同一事务内执行那条确定性动作（不重跑 Agent、不重放历史自由文本调用）
        if approve and executor is not None and proposed:
            try:
                exec_result = executor(proposed)
            except Exception as e:  # noqa: BLE001
                exec_result = {"success": False, "message": f"执行异常：{e}"}
            if not exec_result.get("success"):
                conn.execute(
                    "UPDATE tickets SET status = ?, resolution = ? WHERE ticket_id = ? AND status = ?",
                    (STATE_FAILED, f"审批通过但执行失败：{exec_result.get('message', '')}",
                     ticket_id, STATE_PENDING_REVIEW),
                )
                audit_log(conn, ticket_id=ticket_id, action="approve", actor_id=actor_id,
                          actor_role=actor_role, from_status=cur_status, to_status=STATE_FAILED,
                          reason=f"执行失败：{exec_result.get('message', '')}", outcome="failed",
                          source_trace_id=row["trace_id"], approval_trace_id=approval_trace_id)
                conn.commit()
                return {"success": False, "code": "EXEC_FAILED",
                        "message": f"审批已受理但执行失败：{exec_result.get('message', '')}",
                        "from_status": cur_status, "to_status": STATE_FAILED}
            resolution = exec_result.get("message") or resolution
        elif approve:
            resolution = note or resolution
        else:
            resolution = f"已驳回：{note}" if note else "已驳回"

        to_status = STATE_RESOLVED if approve else STATE_REJECTED
        cur = conn.execute(
            "UPDATE tickets SET status = ?, resolution = ? WHERE ticket_id = ? AND status = ?",
            (to_status, resolution, ticket_id, STATE_PENDING_REVIEW),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return {"success": False, "code": "CONFLICT",
                    "message": "该工单已被其他操作处理（并发审批），本次未重复执行"}

        audit_log(conn, ticket_id=ticket_id, action="approve" if approve else "reject",
                  actor_id=actor_id, actor_role=actor_role, from_status=cur_status,
                  to_status=to_status, reason=note or "", source_trace_id=row["trace_id"],
                  approval_trace_id=approval_trace_id)
        conn.commit()
        return {"success": True, "code": "OK", "message": "已处理",
                "from_status": cur_status, "to_status": to_status}
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return {"success": False, "code": "ERROR", "message": str(e)}
    finally:
        conn.close()


def get_ticket(ticket_id: str) -> dict | None:
    """按 ticket_id 取单条工单（已解析 risk_reasons / proposed_action）。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, ticket_id, user_identifier, user_message, category, status, resolution,"
        " escalate_reason, risk_reasons, proposed_action, created_at, trace_id"
        " FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    item = dict(row)
    for col in ("risk_reasons", "proposed_action"):
        if item.get(col):
            try:
                item[col] = json.loads(item[col])
            except (json.JSONDecodeError, TypeError):
                pass
    return item


def list_audit(ticket_id: str) -> list[dict]:
    """某工单的审计时间线（按发生顺序，只追加不改写）。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM ticket_audit WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_tickets(status: str = "", owner_id: str = "", limit: int = 50) -> list[dict]:
    """工单列表：按状态 / 归属过滤（客户侧传 owner_id 即实现数据边界）。"""
    conn = _get_conn()
    sql = ("SELECT id, ticket_id, user_identifier, user_message, category, status, resolution,"
           " escalate_reason, risk_reasons, proposed_action, created_at, trace_id"
           " FROM tickets WHERE 1=1")
    args: list = []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if owner_id:
        sql += " AND user_identifier = ?"
        args.append(owner_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    conn.close()

    out: list[dict] = []
    for r in rows:
        item = dict(r)
        for col in ("risk_reasons", "proposed_action"):
            if item.get(col):
                try:
                    item[col] = json.loads(item[col])
                except (json.JSONDecodeError, TypeError):
                    pass
        out.append(item)
    return out
