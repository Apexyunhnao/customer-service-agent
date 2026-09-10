"""
一次性迁移脚本：将 mock_db.json 迁移到 SQLite（data/business.db）。
幂等运行 — 表存在则跳过建表，但会清空后重新插入。

时间戳处理：mock_db.json 里的 logistics.updated_at / orders.created_at 是写死的绝对日期，
而业务规则里有"物流停滞超过 N 天 → 转人工"这类**基于当前时间**的判断。若数据固定不变，
随着时间推移所有运单都会被判为停滞，评估结果不可复现。因此迁移时把所有时间戳
**整体平移到当前时间**（保持原来彼此之间的相对新鲜度）。
"""

import json
import sqlite3
import os
import sys
from datetime import datetime, timedelta

# mock 数据的时间基准：数据里最新的一批时间是 2026-08-08 前后
_BASE = datetime(2026, 8, 8, 12, 0, 0)
_SHIFT = datetime.now() - _BASE


def _shift_ts(value: str) -> str:
    """把绝对时间戳按 _SHIFT 平移到当前时间；解析失败则原样返回。"""
    if not value:
        return value
    try:
        return (datetime.strptime(value, "%Y-%m-%d %H:%M:%S") + _SHIFT).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value

DB_JSON = os.path.join(os.path.dirname(__file__), "mock_db.json")
DB_SQLITE = os.path.join(os.path.dirname(__file__), "business.db")


def create_tables(conn: sqlite3.Connection) -> None:
    """建三张表（幂等）。"""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT,
            address TEXT,
            vip TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            product TEXT,
            amount REAL,
            status TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS logistics (
            tracking_no TEXT PRIMARY KEY,
            order_id TEXT REFERENCES orders(order_id),
            company TEXT,
            status TEXT,
            current_location TEXT,
            updated_at TEXT
        );
    """)
    conn.commit()


def migrate() -> None:
    """主迁移逻辑。"""
    with open(DB_JSON, "r", encoding="utf-8") as f:
        db = json.load(f)

    conn = sqlite3.connect(DB_SQLITE)
    create_tables(conn)

    # 清空已有数据后重新插入（保证幂等）
    # 注意删除顺序：先删引用方（logistics / order_remarks），再删被引用方（orders / customers）
    conn.execute("DELETE FROM logistics")
    conn.execute("DELETE FROM order_remarks")
    conn.execute("DELETE FROM orders")
    conn.execute("DELETE FROM customers")

    # ── customers ──
    for c in db["customers"]:
        conn.execute(
            "INSERT INTO customers (id, name, phone, address, vip) VALUES (?, ?, ?, ?, ?)",
            (c["id"], c["name"], c["phone"], c["address"], c["vip"]),
        )

    # ── orders ──
    for o in db["orders"]:
        conn.execute(
            "INSERT INTO orders (order_id, customer_id, product, amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (o["order_id"], o["customer_id"], o["product"], o["amount"], o["status"], _shift_ts(o["created_at"])),
        )

    # ── logistics ──
    for l in db["logistics"]:
        conn.execute(
            "INSERT INTO logistics (tracking_no, order_id, company, status, current_location, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (l["tracking_no"], l["order_id"], l["company"], l["status"], l["current_location"], _shift_ts(l["updated_at"])),
        )

    conn.commit()

    # ── 打印各表行数 ──
    for table in ("customers", "orders", "logistics"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} 行")

    conn.close()
    print(f"\n迁移完成 → {DB_SQLITE}")


if __name__ == "__main__":
    migrate()
