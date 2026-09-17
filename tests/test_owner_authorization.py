"""授权边界回归测试：请求头 X-Owner-Id 不得覆盖已验签 JWT 里的 owner_id。

对应漏洞（2026-09-16 实测复现）：
    owner_id = request.headers.get("X-Owner-Id") or user["owner_id"] or body.user_identifier
请求头优先级最高 ⇒ 任何持有合法 JWT 的调用方（哪怕只是普通客户账号），
只要加一个 `X-Owner-Id` 头，就能以他人身份建单、读取他人历史。

本测试在**修复前的代码上会失败**（归属变成 victim），修复后通过 —— 这是它的全部意义。
"""
import os
import sys
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt as pyjwt
from fastapi.testclient import TestClient

import main as main_mod
from auth import SECRET_KEY

client = TestClient(main_mod.app)

SELF_OWNER = "test-self-owner"
VICTIM_OWNER = "test-victim-owner"

FAKE_RESULT = {
    "category": "咨询",
    "status": "已解决",
    "resolution": "测试占位结果",
    "escalate_reason": "",
    "proposed_action": None,
    "risk_reasons": None,
}


def _token(owner_id: str, role: str = "customer") -> str:
    return pyjwt.encode(
        {"sub": "test-user", "role": role, "owner_id": owner_id,
         "exp": int(time.time()) + 600},
        SECRET_KEY, algorithm="HS256",
    )


def _post(message: str, token: str, extra_headers: dict | None = None):
    """发一次工单请求；返回 (响应, 流程实际使用的 owner_id, 产生的 ticket_id)。"""
    used = {}

    def fake_run(state):
        used["owner_id"] = state.get("user_identifier")
        return dict(FAKE_RESULT), []

    headers = {"Authorization": f"Bearer {token}"}
    headers.update(extra_headers or {})
    with patch("main.run_ticket_with_steps", side_effect=fake_run):
        resp = client.post("/api/ticket", json={"message": message}, headers=headers)
    return resp, used.get("owner_id"), (resp.json().get("ticket_id") if resp.status_code == 200 else None)


def _cleanup(ticket_ids):
    """删除测试产生的工单，避免污染演示数据。"""
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tickets.db")
    if not os.path.exists(db):
        return 0
    con = sqlite3.connect(db)
    n = 0
    for tid in [t for t in ticket_ids if t]:
        n += con.execute("DELETE FROM tickets WHERE ticket_id = ?", (tid,)).rowcount
    con.commit()
    con.close()
    return n


def main_test() -> int:
    passed = failed = 0
    made = []

    def check(label, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  ✅ {label}")
        else:
            failed += 1
            print(f"  ❌ {label}" + (f" —— {detail}" if detail else ""))

    print("=" * 74)
    print("1) 伪造 X-Owner-Id 头 —— 不得改变归属")
    print("=" * 74)
    tag = f"AUTHTEST-{uuid.uuid4().hex[:8]}"
    resp, used, tid = _post(tag + " 测试消息", _token(SELF_OWNER),
                            {"X-Owner-Id": VICTIM_OWNER})
    made.append(tid)
    check("请求成功", resp.status_code == 200, f"status={resp.status_code}")
    check("归属 = 自己的 JWT 值（伪造头被忽略）",
          used == SELF_OWNER,
          f"实际={used!r}，说明 X-Owner-Id 仍能覆盖身份")

    print()
    print("=" * 74)
    print("2) 无伪造头时 —— 归属来自 JWT")
    print("=" * 74)
    resp2, used2, tid2 = _post(tag + " 第二条", _token(SELF_OWNER))
    made.append(tid2)
    check("归属 = 自己的 JWT 值", used2 == SELF_OWNER, f"实际={used2!r}")

    print()
    print("=" * 74)
    print("3) 请求体里的 user_identifier 也不得作为身份依据")
    print("=" * 74)
    used3 = {}

    def fake_run3(state):
        used3["owner_id"] = state.get("user_identifier")
        return dict(FAKE_RESULT), []

    with patch("main.run_ticket_with_steps", side_effect=fake_run3):
        resp3 = client.post(
            "/api/ticket",
            json={"message": f"{tag} 第三条", "user_identifier": VICTIM_OWNER},
            headers={"Authorization": f"Bearer {_token(SELF_OWNER)}"},
        )
    if resp3.status_code == 200:
        made.append(resp3.json().get("ticket_id"))
    check("归属仍 = JWT 值（请求体字段被忽略）",
          used3.get("owner_id") == SELF_OWNER,
          f"实际={used3.get('owner_id')!r}")

    print()
    print("=" * 74)
    print("4) 未登录 —— 401")
    print("=" * 74)
    r4 = client.post("/api/ticket", json={"message": tag + " 未登录"})
    check("未带凭证 → 401", r4.status_code == 401, f"status={r4.status_code}")

    print()
    print("=" * 74)
    print("5) 用别的密钥签的 token —— 401（伪造签名被拒）")
    print("=" * 74)
    forged = pyjwt.encode({"sub": "x", "role": "customer", "owner_id": VICTIM_OWNER,
                           "exp": int(time.time()) + 600}, "wrong-secret", algorithm="HS256")
    r5 = client.post("/api/ticket", json={"message": tag + " 伪造签名"},
                     headers={"Authorization": f"Bearer {forged}"})
    check("伪造签名 → 401", r5.status_code == 401, f"status={r5.status_code}")

    n = _cleanup(made)
    print()
    print(f"（已清理测试工单 {n} 条，保持演示数据干净）")

    print()
    print("=" * 74)
    print(f"结果：{passed}/{passed + failed} 通过")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_test())
