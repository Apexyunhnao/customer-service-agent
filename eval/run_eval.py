"""
评估脚本 — 跑 test_cases.json 全部 56 条用例，逐条过 graph，
计算分类准确率、行动准确率、自动处理成功率，输出报表。
"""

import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Windows GBK 兜底
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from graph import run_ticket

# ── 加载用例 ────────────────────────────────────────────────────

CASE_SET = "train"  # 默认只跑训练集
for i, arg in enumerate(sys.argv):
    if arg == "--cases" and i + 1 < len(sys.argv):
        CASE_SET = sys.argv[i + 1]
    elif arg == "--holdout":
        CASE_SET = "holdout"
    elif arg == "--all":
        CASE_SET = "all"

CASE_FILES = {
    "train": Path(__file__).parent / "train_cases.json",
    "holdout": Path(__file__).parent / "holdout_cases.json",
    "all": Path(__file__).parent / "test_cases.json",
}
test_path = CASE_FILES.get(CASE_SET, CASE_FILES["train"])
with open(test_path, "r", encoding="utf-8") as f:
    test_cases = json.load(f)

print(f"用例集: {CASE_SET} ({len(test_cases)} 条)")

# ── 评估逻辑 ────────────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # 秒，指数退避：2, 4, 8


def _is_retryable(error: Exception) -> bool:
    """判断异常是否可重试：连接错误、超时、5xx。"""
    msg = str(error).lower()
    # OpenAI / httpx 常见可重试异常
    retryable_patterns = [
        "connection", "timeout", "timed out", "server error",
        "500", "502", "503", "504", "rate limit", "too many requests",
        "remote disconnect", "reset by peer", "service unavailable",
    ]
    return any(p in msg for p in retryable_patterns)


def invoke_with_retry(state: dict) -> dict:
    """调用 run_ticket，遇可重试异常时最多重试 MAX_RETRIES 次。"""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return run_ticket(state)
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES and _is_retryable(e):
                delay = RETRY_BASE_DELAY ** attempt
                print(f"    [重试] 第 {attempt} 次失败，{delay}s 后重试: {str(e)[:80]}")
                time.sleep(delay)
            else:
                break
    raise last_error  # type: ignore[misc]


results: list[dict] = []
category_stats: dict[str, dict] = defaultdict(lambda: {
    "total": 0, "category_ok": 0, "action_ok": 0, "auto_ok": 0, "auto_total": 0,
})

start_time = time.time()

for idx, tc in enumerate(test_cases):
    ticket_id = f"EVAL-{datetime.now().strftime('%Y%m%d%H%M%S')}-{idx+1:03d}"

    state = {
        "ticket_id": ticket_id,
        "user_message": tc["message"],
        "category": "",
        "extracted_info": {},
        "tool_results": [],
        "resolution": "",
        "status": "新建",
        "escalate_reason": "",
        "history": [],
    }

    try:
        result = invoke_with_retry(state)
    except Exception as e:
        result = {
            "category": "",
            "status": "已关闭",
            "escalate_reason": f"图执行异常: {e}",
            "resolution": str(e),
            "tool_results": [],
        }

    # 判断实际行为
    actual_action = "escalate" if result.get("escalate_reason", "") else "auto"
    actual_category = result.get("category", "")
    actual_tools = [r.get("tool_name", "") for r in result.get("tool_results", [])]

    # 各项准确性
    action_ok = actual_action == tc["expected_action"]
    category_ok = actual_category == tc["category"]

    # 工具合理性：actual_tools 要包含 expected_tools 里的全部工具
    # 转人工的用例不要求工具全调用（因为可能根本没调工具就转了）
    if tc["expected_action"] == "escalate":
        tools_ok = None  # 不评判
    else:
        missing = set(tc["expected_tools"]) - set(actual_tools)
        tools_ok = len(missing) == 0

    # 自动处理成功：预期 auto 且实际 auto
    auto_success = (tc["expected_action"] == "auto" and actual_action == "auto")

    record = {
        "id": tc["id"],
        "category_expected": tc["category"],
        "category_actual": actual_category,
        "action_expected": tc["expected_action"],
        "action_actual": actual_action,
        "expected_tools": tc["expected_tools"],
        "actual_tools": actual_tools,
        "tools_ok": tools_ok,
        "action_ok": action_ok,
        "category_ok": category_ok,
        "auto_success": auto_success,
        "escalate_reason": result.get("escalate_reason", "")[:120],
        "message": tc["message"],
    }
    results.append(record)

    # 分类别统计
    cat = tc["category"]
    category_stats[cat]["total"] += 1
    if category_ok:
        category_stats[cat]["category_ok"] += 1
    if action_ok:
        category_stats[cat]["action_ok"] += 1
    if tc["expected_action"] == "auto":
        category_stats[cat]["auto_total"] += 1
        if auto_success:
            category_stats[cat]["auto_ok"] += 1

    # 进度
    if (idx + 1) % 10 == 0:
        print(f"  进度: {idx+1}/{len(test_cases)}")

elapsed = time.time() - start_time

# ── 汇总指标 ────────────────────────────────────────────────────

total = len(results)
total_auto_expected = sum(1 for tc in test_cases if tc["expected_action"] == "auto")
total_escalate_expected = sum(1 for tc in test_cases if tc["expected_action"] == "escalate")
auto_success_count = sum(1 for r in results if r["auto_success"])
escalate_actual_count = sum(1 for r in results if r["action_actual"] == "escalate")
category_ok_count = sum(1 for r in results if r["category_ok"])
action_ok_count = sum(1 for r in results if r["action_ok"])
tools_ok_count = sum(1 for r in results if r["tools_ok"] is True)
tools_total = sum(1 for r in results if r["tools_ok"] is not None)

failed_cases = [r for r in results if not r["action_ok"] or not r["category_ok"] or r["tools_ok"] is False]

summary = {
    "total": total,
    "auto_expected": total_auto_expected,
    "escalate_expected": total_escalate_expected,
    "auto_success_count": auto_success_count,
    "escalate_actual_count": escalate_actual_count,
    "escalate_rate": f"{escalate_actual_count / total * 100:.1f}%",
    "category_accuracy": f"{category_ok_count / total * 100:.1f}%",
    "action_accuracy": f"{action_ok_count / total * 100:.1f}%",
    "auto_success_rate": f"{auto_success_count / total_auto_expected * 100:.1f}%" if total_auto_expected else "N/A",
    "tools_accuracy": f"{tools_ok_count / tools_total * 100:.1f}%" if tools_total else "N/A",
    "by_category": {
        cat: {
            "total": v["total"],
            "category_accuracy": f"{v['category_ok'] / v['total'] * 100:.1f}%",
            "action_accuracy": f"{v['action_ok'] / v['total'] * 100:.1f}%",
            "auto_success_rate": f"{v['auto_ok'] / v['auto_total'] * 100:.1f}%" if v["auto_total"] else "N/A",
        }
        for cat, v in sorted(category_stats.items())
    },
    "failed_count": len(failed_cases),
    "failed_cases": failed_cases,
    "elapsed_seconds": round(elapsed, 1),
}

# ── 打印报表 ────────────────────────────────────────────────────

print()
print("=" * 64)
print("  评 估 报 表")
print("=" * 64)
print(f"  总用例数:        {total}")
print(f"  预期 auto:       {total_auto_expected}")
print(f"  预期 escalate:   {total_escalate_expected}")
print(f"  实际 auto:       {auto_success_count}")
print(f"  实际 escalate:   {escalate_actual_count}")
print(f"  转人工率:        {summary['escalate_rate']}")
print(f"  耗时:            {elapsed:.1f}s")
print()
print(f"  分类准确率:      {summary['category_accuracy']}  ({category_ok_count}/{total})")
print(f"  行动准确率:      {summary['action_accuracy']}  ({action_ok_count}/{total})")
print(f"  自动处理成功率:  {summary['auto_success_rate']}  ({auto_success_count}/{total_auto_expected})")
if tools_total:
    print(f"  工具覆盖准确率:  {summary['tools_accuracy']}  ({tools_ok_count}/{tools_total})")
print()
print("  按类别统计：")
print(f"  {'类别':<6} {'数量':<6} {'分类准确':<10} {'行动准确':<10} {'自动成功率':<12}")
print(f"  {'─'*6} {'─'*6} {'─'*10} {'─'*10} {'─'*12}")
for cat in ["订单", "物流", "售后"]:
    if cat in summary["by_category"]:
        s = summary["by_category"][cat]
        print(f"  {cat:<6} {s['total']:<6} {s['category_accuracy']:<10} {s['action_accuracy']:<10} {s['auto_success_rate']:<12}")

if failed_cases:
    print()
    print(f"  失败用例 ({len(failed_cases)} 条)：")
    print(f"  {'ID':<10} {'预期':<10} {'实际':<10} {'分类对':<8} {'行动对':<8} {'工具对':<8} 用户消息")
    print(f"  {'─'*10} {'─'*10} {'─'*10} {'─'*8} {'─'*8} {'─'*8} {'─'*40}")
    for r in failed_cases:
        print(f"  {r['id']:<10} {r['action_expected']:<10} {r['action_actual']:<10} "
              f"{'Y' if r['category_ok'] else 'N':<8} "
              f"{'Y' if r['action_ok'] else 'N':<8} "
              f"{r['tools_ok'] if r['tools_ok'] is not None else '-':<8} "
              f"{r['message'][:40]}")

print()
print(f"  报表已保存到: eval/results.json")
print("=" * 64)

# ── 保存结果 ────────────────────────────────────────────────────

OUTPUT_PATH = Path(__file__).parent / "results.json"
with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

# ── 失败分析 ────────────────────────────────────────────────────

analysis_items: list[str] = []

if failed_cases:
    # 挑选 3 个最有代表性的失败用例
    # 优选取样：1 个分类错误、1 个行动错误、1 个工具不全
    picked: list[dict] = []

    category_fails = [r for r in failed_cases if not r["category_ok"]]
    action_fails = [r for r in failed_cases if not r["action_ok"]]
    tools_fails = [r for r in failed_cases if r["tools_ok"] is False]

    for pool in [category_fails, action_fails, tools_fails]:
        if pool and len(picked) < 3:
            for r in pool:
                if r not in picked:
                    picked.append(r)
                    break

    # 补齐到 3 个
    for r in failed_cases:
        if len(picked) >= 3:
            break
        if r not in picked:
            picked.append(r)

    for i, r in enumerate(picked[:3], 1):
        analysis_items.append(f"""### 失败用例 {i}: {r['id']}

**用户消息**: {r['message']}

**期望 vs 实际**:
- 期望分类: {r['category_expected']} | 实际分类: {r['category_actual']} {'✓' if r['category_ok'] else '✗'}
- 期望操作: {r['action_expected']} | 实际操作: {r['action_actual']} {'✓' if r['action_ok'] else '✗'}
- 期望工具: {r['expected_tools']} | 实际工具: {r['actual_tools']} ({r['tools_ok'] if r['tools_ok'] is not None else 'N/A'})
- 转人工原因: {r.get('escalate_reason', '')}

**失败原因分析**:
""")

        # 生成具体分析
        reasons = []
        if not r["category_ok"]:
            reasons.append(
                "**分类错误**：LLM 将 [" + str(r['category_expected']) + "] 类误判为 ["
                + str(r['category_actual']) + "] 类。"
                + "可能原因：(1) prompt 中类别边界不够清晰，用户问'到哪了'实际是查物流，"
                + "但 LLM 可能根据订单号信息优先判定为订单类；"
                + "(2) 中文口语表达的多义性导致分类歧义。"
            )
        if not r["action_ok"]:
            if r["action_expected"] == "escalate" and r["action_actual"] == "auto":
                reasons.append(
                    "**应转未转**：边界用例，LLM 未识别出应转人工的条件。"
                    + "可能原因：(1) 工具层安全边界返回了 success=false，但 LLM 未调用相关工具；"
                    + "(2) 隐式转人工条件（语气强烈、诈骗风险）无法通过工具返回值捕获，"
                    + "需在 decide_node 中增加敏感词/规则检测。"
                )
            elif r["action_expected"] == "auto" and r["action_actual"] == "escalate":
                reasons.append(
                    "**误转人工**：本应自动处理的用例被错误转人工。"
                    + "可能原因：(1) LLM 未选出正确工具导致 tool_results 为空；"
                    + "(2) 工具调用参数错误（如金额传了 null）使工具返回 success=false；"
                    + "(3) classify_node 遗漏关键字段。"
                )
        if r["tools_ok"] is False:
            expected = set(r["expected_tools"])
            actual = set(r["actual_tools"])
            missing = expected - actual
            extra = actual - expected
            parts = []
            if missing:
                parts.append("缺少工具: " + str(missing))
            if extra:
                parts.append("多余工具: " + str(extra))
            reasons.append(
                "**工具选择偏差**: " + "; ".join(parts) + "。"
                + "可能原因：(1) LLM 未严格按 category + extracted_info 选择工具；"
                + "(2) prompt 中工具描述不够明确；"
                + "(3) 需要更显式的规则引导。"
            )

        if not reasons:
            reasons.append("综合因素导致，需结合具体上下文分析。")

        for reason in reasons:
            analysis_items[-1] += "- " + reason + "\n"

        analysis_items[-1] += "\n**改进建议**:\n"

        suggestions = []
        if not r["category_ok"]:
            suggestions.append(
                "- **优化 classify_node prompt**: 在 system prompt 中明确"
                + "查物流优先于查订单的判定规则，"
                + "当用户问到哪了/发了吗时优先分类为物流"
            )
        if not r["action_ok"] and r["action_expected"] == "escalate":
            suggestions.append(
                "- **增加转人工规则检测**: 在 decide_node 中不只看工具返回值，"
                + "还要加一级规则检查，如检测到投诉关键词(12315/曝光/投诉)、"
                + "诈骗风险、订单模糊不清等，直接转人工"
            )
        if not r["action_ok"] and r["action_expected"] == "auto":
            suggestions.append(
                "- **加强 classify_node 信息提取**: 确保订单号/运单号/金额等"
                + "关键字段被正确提取，为空时给 handle_node 明确的信号而非直接放弃"
            )
        if r["tools_ok"] is False:
            suggestions.append(
                "- **细化 handle_node prompt**: 添加更多 few-shot 示例，"
                + "让 LLM 更准确理解 query_order 与 query_logistics 的对应关系"
            )

        analysis_items[-1] += "\n".join(suggestions) if suggestions else "- 需人工复查具体上下文"
        analysis_items[-1] += "\n"

DEFAULT_ANALYSIS = ""  # 运行时动态生成，见下文

analysis_md = f"""# 评估失败分析

> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
> 评估结果：{summary['action_accuracy']} 行动准确率，{summary['category_accuracy']} 分类准确率

## 总体情况

- 总用例 {total} 条，失败 {len(failed_cases)} 条
- 分类错误 {total - category_ok_count} 条，行动错误 {total - action_ok_count} 条
- 工具覆盖偏差 {tools_total - tools_ok_count} 条（共 {tools_total} 条 auto 用例评估了工具）

---

{chr(10).join(analysis_items) if analysis_items else ("## 无失败用例" + chr(10) + chr(10) + "所有 " + str(total) + " 条用例全部通过，无需分析。")}

## 总结

"""

if failed_cases:
    category_fail_count = sum(1 for r in failed_cases if not r["category_ok"])
    action_fail_count = sum(1 for r in failed_cases if not r["action_ok"])
    tools_fail_count = sum(1 for r in failed_cases if r["tools_ok"] is False)

    analysis_md += f"- 分类错误 {category_fail_count} 条 → 优先优化 classify_node 的 prompt 和 few-shot 示例\n"
    analysis_md += f"- 行动错误 {action_fail_count} 条 → 重点排查 decide_node 的边界逻辑和敏感词检测\n"
    analysis_md += f"- 工具偏差 {tools_fail_count} 条 → 细化 handle_node 中的工具选择指引\n"
else:
    analysis_md += str(total) + " 条用例全部通过。\n"

ANALYSIS_PATH = Path(__file__).parent / "failure_analysis.md"
with open(ANALYSIS_PATH, "w", encoding="utf-8") as f:
    f.write(analysis_md)

print(f"\n失败分析已保存到: eval/failure_analysis.md")
