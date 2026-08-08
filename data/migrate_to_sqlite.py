"""
一次性迁移脚本：将 mock_db.json 迁移到 SQLite（data/business.db）。
幂等运行 — 表存在则跳过建表，但会清空后重新插入。
"""

import json
import sqlite3
import os
import sys

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
    conn.execute("DELETE FROM logistics")
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
            (o["order_id"], o["customer_id"], o["product"], o["amount"], o["status"], o["created_at"]),
        )

    # ── logistics ──
    for l in db["logistics"]:
        conn.execute(
            "INSERT INTO logistics (tracking_no, order_id, company, status, current_location, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (l["tracking_no"], l["order_id"], l["company"], l["status"], l["current_location"], l["updated_at"]),
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
