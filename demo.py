"""
Demo 脚本 — 取 eval/test_cases.json 前 3 条用例跑完整流程，
打印每一步的 state 变化，最后保存工单到 SQLite。
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

# Windows 控制台 GBK 编码兜底，防止打印中文/特殊符号时崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from graph import run_ticket
from db import create_ticket, get_user_history

# 读测试用例
test_cases_path = Path(__file__).parent / "eval" / "test_cases.json"
with open(test_cases_path, "r", encoding="utf-8") as f:
    all_cases = json.load(f)

demo_cases = all_cases[:3]

print("=" * 60)
print("客服工单处理 Agent — Demo 运行")
print(f"测试用例：前 {len(demo_cases)} 条")
print(f"DEEPSEEK_API_KEY 已设置：{'是' if os.environ.get('DEEPSEEK_API_KEY') else '否'}")
print("=" * 60)

if not os.environ.get("DEEPSEEK_API_KEY"):
    print("\n[!] 未设置 DEEPSEEK_API_KEY 环境变量，LLM 调用将失败。")
    print("请在项目根目录创建 .env 文件并写入: DEEPSEEK_API_KEY=your-key")
    sys.exit(1)

for i, tc in enumerate(demo_cases, 1):
    ticket_id = f"DEMO-{datetime.now().strftime('%Y%m%d%H%M%S')}-{i:03d}"
    print(f"\n{'─' * 60}")
    print(f"【用例 {i}】{tc['id']} | 预期：{tc['expected_action']}")
    print(f"用户消息：{tc['message']}")
    print(f"{'─' * 60}")

    # 初始 state
    state = {
        "ticket_id": ticket_id,
        "user_message": tc["message"],
        "category": "",
        "extracted_info": {},
        "tool_results": [],
        "resolution": "",
        "status": "新建",
        "escalate_reason": "",
        "history": get_user_history("13800138001"),  # 用固定用户查历史
    }

    print("\n[运行中] 调用 Graph...")
    try:
        result = run_ticket(state)
    except Exception as e:
        print(f"\n[错误] 图运行异常：{e}")
        continue

    print(f"\n[结果] 最终 state：")
    print(f"  category:      {result.get('category', '')}")
    print(f"  status:        {result.get('status', '')}")
    print(f"  extracted_info:{json.dumps(result.get('extracted_info', {}), ensure_ascii=False)}")
    print(f"  escalate_reason: {result.get('escalate_reason', '')[:80]}")
    print(f"  tool_results ({len(result.get('tool_results', []))} 条):")
    for tr in result.get("tool_results", []):
        print(f"    - {tr.get('tool_name','?')}: success={tr.get('success')}, {tr.get('message','')[:60]}")
    resolution = result.get('resolution', '')
    print(f"  resolution:    {resolution[:120]}{'...' if len(resolution) > 120 else ''}")

    # 保存到数据库
    db_result = create_ticket(
        ticket_id=ticket_id,
        user_message=tc["message"],
        user_identifier="13800138001",
        category=result.get("category", ""),
        status=result.get("status", "已关闭"),
        resolution=result.get("resolution", ""),
        escalate_reason=result.get("escalate_reason", ""),
    )
    print(f"\n[DB] 数据库保存：{'成功' if db_result.get('success') else '失败'}（{ticket_id}）")

print(f"\n{'=' * 60}")
print("Demo 完成")
print(f"{'=' * 60}")
