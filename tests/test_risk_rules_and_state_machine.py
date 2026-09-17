"""风控规则与工单状态机回归测试 —— 锁定现有确定性规则的行为，不改任何生产代码。

被测对象（全部是纯函数 / 纯常量，不触发 LLM、不连数据库、不发网络请求、不写文件）：

    nodes.py  _RISK_KEYWORDS / _LOGISTICS_EMOTION_KEYWORDS
              _has_emotion(msg) -> bool            物流情绪词检测
              _check_fraud(msg) -> str | None      防诈骗组合关键词检测
              _check_logistics_stale(results)      物流停滞检测（见下方「冻结时间」）
              _detect_risks(state) -> list[str]    风险总入口，五条规则按序执行
    db.py     can_transition(from, to) -> bool
              ALLOWED_TRANSITIONS / ALL_TICKET_STATES / STATE_*

为什么需要这个文件：这五条风险规则是**写操作之前的唯一门禁**（命中即拦下退款/改地址等副作用，
转人工审批），规则表被改宽或改窄都意味着安全边界位移，但没有任何测试盯住它。
本文件用「正例 + 负例 + 边界」把当前行为钉死：将来谁改动了关键词或状态机，
这里必须先失败，改动才是有意识的。

五条规则**全部 5/5 已覆盖**，包括内部调用 datetime.now() 的 `_check_logistics_stale`
（第 6 节）。它靠「冻结时间」做到确定性：用 unittest.mock.patch 把 `nodes.datetime`
换成替身，`now()` 恒返回固定时刻 FROZEN_NOW，`strptime` 仍走真实实现，
因此**不依赖真实时钟、跨午夜也不会抖**，同时不修改任何生产代码。

另有一组转换（待审核 → 处理失败）属**未决 Known Issue**，本文件**不对它做正误判定**，
见第 7 节内联注释与 E:\AI-Bridge\docs\KnownIssues-customer-service-agent-20260918.md。

运行方式（不需要 pytest，也不需要 DEEPSEEK_API_KEY）：

    python tests/test_risk_rules_and_state_machine.py     # 退出码 0=全过 / 1=有失败

"""  # noqa: D400
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nodes  # noqa: E402

from nodes import (  # noqa: E402
    _RISK_KEYWORDS,
    _LOGISTICS_EMOTION_KEYWORDS,
    _has_emotion,
    _check_fraud,
    _detect_risks,
)
from db import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    ALL_TICKET_STATES,
    STATE_FAILED,
    STATE_NEW,
    STATE_PENDING_REVIEW,
    STATE_PROCESSING,
    STATE_REJECTED,
    STATE_RESOLVED,
    can_transition,
)

# 说明：导入 nodes 会执行 load_dotenv()，但不会创建 LLM 实例（_get_llm 是延迟初始化），
# 因此没有 DEEPSEEK_API_KEY 也能导入并跑完本文件。

# 规则表内容 + 顺序的期望值。
# 顺序是有语义的：_detect_risks 的敏感词检测命中即 break，只报**第一个**命中的词，
# 所以列表顺序决定了用户最终看到哪一条风险描述。顺序变化 = 可观测行为变化。
EXPECTED_RISK_KEYWORDS = [
    "12315", "315", "投诉", "赔偿", "假货", "翻新", "骗子", "诈骗",
    "气死", "太垃圾", "曝光", "等着吧", "报警", "法院", "律师",
]
EXPECTED_EMOTION_KEYWORDS = [
    "丢了吧", "不能耽误", "急", "赶紧", "说法", "投诉",
    "丢了", "赔", "损失", "不行", "急死",
]

# 防诈骗规则里写死的 8 组组合（见 nodes._check_fraud），此处独立抄一份用于锁定。
EXPECTED_FRAUD_PAIRS = [
    ("接到电话", "快递丢"),
    ("收到短信", "快递丢"),
    ("接到电话", "核实信息"),
    ("收到短信", "核实信息"),
    ("电话说", "丢"),
    ("短信说", "丢"),
    ("打电话", "丢件"),
    ("打电话", "快递丢了"),
]

# 状态机的合法转换，独立抄一份（而不是直接遍历 ALLOWED_TRANSITIONS，
# 否则测试只是复述实现，改宽了也不会失败）。
LEGAL_TRANSITIONS = {
    (STATE_NEW, STATE_PROCESSING),
    (STATE_PROCESSING, STATE_RESOLVED),
    (STATE_PROCESSING, STATE_PENDING_REVIEW),
    (STATE_PROCESSING, STATE_FAILED),
    (STATE_PENDING_REVIEW, STATE_RESOLVED),
    (STATE_PENDING_REVIEW, STATE_REJECTED),
}


def _logistics(tracking_no: str, status: str) -> dict:
    """造一条物流查询结果；**不带 updated_at** → _check_logistics_stale 会跳过。

    这让第 4/5 节的断言不受「当前时间」影响（规则 3 不会被误触发，其余四条规则
    可以单独观测）。规则 3 本身在第 6 节用冻结时间（见 _frozen_time）单独测。
    """
    return {"tool_name": "query_logistics",
            "data": {"tracking_no": tracking_no, "status": status}}


# ── 冻结时间：让 _check_logistics_stale 变成确定性函数 ──────────────────────
#
# nodes.py 里是 `from datetime import datetime, timedelta`，所以模块属性 nodes.datetime
# 是**类本身**，可以直接被 patch 成替身。替身只需满足两点：
#   1. now() 恒返回同一个固定时刻（阈值判断与 days_stale 用的是同一个「现在」）
#   2. strptime() 仍走真实实现（保留 strptime，替身才解析得了时间戳）
# timedelta 未被 patch，仍是真实的 timedelta，因此 `now() - timedelta(days=3)` 正常。
LOGISTICS_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# 冻结时刻：选一个与真实「今天」无关的固定日期，避免测试随运行日期漂移。
FROZEN_NOW = datetime(2026, 9, 18, 12, 0, 0)

# _LOGISTICS_STALE_DAYS = 3（nodes.py），此处独立抄一份用于构造边界数据。
STALE_THRESHOLD_DAYS = 3


def _frozen_time(now: datetime):
    """返回一个 patch 上下文：把 nodes.datetime 换成 now() 可控的替身。"""

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None) -> datetime:
            return now

        # 真实实现，替身才不会把合法时间戳也判成解析失败
        strptime = staticmethod(datetime.strptime)

    return patch.object(nodes, "datetime", _FrozenDateTime)


def _to_ts(dt: datetime) -> str:
    """渲染成 _check_logistics_stale 唯一接受的格式。"""
    return dt.strftime(LOGISTICS_TS_FORMAT)


def _logistics_updated(tracking_no: str, status: str, updated_at) -> dict:
    """带 updated_at 的物流结果；只在 _frozen_time 上下文里使用才有确定性。"""
    return {"tool_name": "query_logistics",
            "data": {"tracking_no": tracking_no, "status": status,
                     "updated_at": updated_at}}


def _order(order_id: str, order_status: str) -> dict:
    """造一条订单查询结果（不带 status 字段，避免与物流规则互相污染）。"""
    return {"tool_name": "query_order",
            "data": {"order_id": order_id, "order_status": order_status}}


def main_test() -> int:
    passed = failed = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  ✅ {label}")
        else:
            failed += 1
            print(f"  ❌ {label}" + (f" —— {detail}" if detail else ""))

    def section(title: str) -> None:
        print()
        print("=" * 74)
        print(title)
        print("=" * 74)

    # ── 1. 关键词常量 ────────────────────────────────────────────
    section("1) 关键词常量 —— 内容与顺序锁定（顺序决定敏感词只报哪一条）")

    check("_RISK_KEYWORDS 内容与顺序与现状完全一致",
          _RISK_KEYWORDS == EXPECTED_RISK_KEYWORDS,
          f"实际={_RISK_KEYWORDS!r}")
    check("_RISK_KEYWORDS 无重复项（重复项=永远不会被报出的死词）",
          len(set(_RISK_KEYWORDS)) == len(_RISK_KEYWORDS))
    check("_LOGISTICS_EMOTION_KEYWORDS 内容与顺序与现状完全一致",
          _LOGISTICS_EMOTION_KEYWORDS == EXPECTED_EMOTION_KEYWORDS,
          f"实际={_LOGISTICS_EMOTION_KEYWORDS!r}")
    check("_LOGISTICS_EMOTION_KEYWORDS 无重复项",
          len(set(_LOGISTICS_EMOTION_KEYWORDS)) == len(_LOGISTICS_EMOTION_KEYWORDS))

    # ── 2. _has_emotion ─────────────────────────────────────────
    section("2) _has_emotion —— 物流情绪词：命中 / 不命中 / 边界")

    check("命中「急」（单字子串即可命中）", _has_emotion("我的快递呢，很急") is True)
    check("命中「丢了吧」（多字词组）", _has_emotion("快递丢了吧") is True)
    check("命中「投诉」", _has_emotion("再不处理我就投诉") is True)
    check("命中「不行」", _has_emotion("这样不行") is True)
    # 负例：一句正常咨询里不含任何情绪词，不得被判为有情绪
    check("不命中：普通物流咨询",
          _has_emotion("请问我的包裹什么时候能到") is False,
          "普通咨询被误判为情绪信号会白白转人工")
    check("不命中：空消息（不能因为空串就判有情绪）",
          _has_emotion("") is False)
    # 边界（可疑行为，见文末「观察记录」第 1 条）：语义相反的「不着急」同样命中「急」
    check("[锁定现状] 语义相反的「不着急」也会命中（单字「急」过宽）",
          _has_emotion("我不着急，慢慢来") is True)

    # ── 3. _check_fraud ─────────────────────────────────────────
    section("3) _check_fraud —— 防诈骗：必须 a、b 同时出现才触发")

    for a, b in EXPECTED_FRAUD_PAIRS:
        msg = f"我{a}了，他说{b}"
        reason = _check_fraud(msg)
        check(f"组合命中：「{a}」+「{b}」",
              bool(reason) and a in reason and b in reason,
              f"返回={reason!r}")

    check("只命中前半「接到电话」→ 不触发",
          _check_fraud("我接到电话") is None,
          "半截匹配就报诈骗会造成大面积误报")
    check("只命中后半「快递丢了」→ 不触发",
          _check_fraud("我的快递丢了") is None)
    check("只命中前半「收到短信」→ 不触发",
          _check_fraud("我收到短信了") is None)
    check("只命中后半「核实信息」→ 不触发",
          _check_fraud("对方让我核实信息") is None)
    check("空消息 → 不触发", _check_fraud("") is None)
    check("命中文案包含「诈骗」与「人工」两个关键词（供客服直接读懂）",
          "诈骗" in (_check_fraud("我接到电话说快递丢了") or "")
          and "人工" in (_check_fraud("我接到电话说快递丢了") or ""))

    # ── 4. _detect_risks：单条规则 ───────────────────────────────
    section("4) _detect_risks —— 单条规则正例 / 负例")

    # 4.1 敏感词
    r = _detect_risks({"user_message": "再不解决我就要投诉你们", "tool_results": []})
    check("敏感词命中：「投诉」→ 1 条风险，且文案含「投诉」",
          len(r) == 1 and "投诉" in r[0], f"实际={r!r}")

    r = _detect_risks({"user_message": "请问订单什么时候发货", "tool_results": []})
    check("敏感词未命中：普通咨询 → 无风险",
          r == [], f"实际={r!r}")

    # 命中即 break：列表里「投诉」在「赔偿」之前，因此只报「投诉」
    r = _detect_risks({"user_message": "我要投诉，并且要求赔偿", "tool_results": []})
    check("敏感词命中即 break：同时含「投诉」「赔偿」只报第一个「投诉」",
          len(r) == 1 and "投诉" in r[0] and "赔偿" not in r[0],
          f"实际={r!r}")

    # 4.2 防诈骗（单独触发）
    r = _detect_risks({"user_message": "我刚接到电话说我的快递丢了", "tool_results": []})
    check("防诈骗单独命中 → 1 条风险，且文案含「诈骗」",
          len(r) == 1 and "诈骗" in r[0], f"实际={r!r}")

    # 4.3 物流异常 + 情绪 → 触发
    r = _detect_risks({"user_message": "我的快递到底在哪，急死了",
                       "tool_results": [_logistics("SF1001", "异常")]})
    check("物流异常 + 情绪词 → 触发「物流状态异常」",
          len(r) == 1 and "物流状态异常" in r[0], f"实际={r!r}")

    # 4.4 物流异常但无情绪 → 不触发这一条（规则 4 的负例，最容易写错的地方）
    r = _detect_risks({"user_message": "帮我看下物流进度",
                       "tool_results": [_logistics("SF1001", "异常")]})
    check("物流异常但用户无情绪 → 不触发（规则 4 要求两者同时成立）",
          r == [], f"实际={r!r}")

    # 4.5 运输中 + 情绪 → 也不触发规则 4（规则 4 只认「异常」，不认「运输中」）
    r = _detect_risks({"user_message": "我的快递到底在哪，急死了",
                       "tool_results": [_logistics("SF1001", "运输中")]})
    check("[锁定现状] 运输中 + 情绪 → 不触发规则 4（只认「异常」状态）",
          r == [], f"实际={r!r}")

    # 4.6 已取消订单 + 退款诉求 → 触发
    r = _detect_risks({"user_message": "订单被取消了，我要退款",
                       "tool_results": [_order("ORD-1001", "已取消")]})
    check("已取消订单 + 退款诉求 → 触发「订单已取消但用户要求退款」",
          len(r) == 1 and "已取消" in r[0], f"实际={r!r}")

    r = _detect_risks({"user_message": "我的订单被取消了，怎么回事",
                       "tool_results": [_order("ORD-1001", "已取消")]})
    check("已取消订单但没提退款 → 不触发（规则 5 要求两者同时成立）",
          r == [], f"实际={r!r}")

    r = _detect_risks({"user_message": "订单被取消了，我要退款",
                       "tool_results": [_order("ORD-1001", "已完成")]})
    check("有退款诉求但订单未取消 → 不触发（规则 5 要求两者同时成立）",
          r == [], f"实际={r!r}")

    # 4.7 规则 5 的宽词边界：refund_words 含单字「退」，与退款无关的诉求同样命中
    r = _detect_risks({"user_message": "别给我发短信了，我要退订",
                       "tool_results": [_order("ORD-1001", "已取消")]})
    check("[锁定现状] 单字「退」过宽：「退订」也被当成退款诉求而触发",
          len(r) == 1 and "已取消" in r[0],
          f"实际={r!r}（见「观察记录」第 5 条）")

    # ── 5. _detect_risks：多规则叠加与顺序 ───────────────────────
    section("5) _detect_risks —— 多规则同时触发 + 固定顺序 + 健壮性")

    # 敏感词 + 防诈骗 同时命中（tool_results 为空，避免混入物流/订单规则）
    r = _detect_risks({"user_message": "我接到电话说快递丢了，你们就是诈骗",
                       "tool_results": []})
    check("敏感词 + 防诈骗 同时命中 → 2 条风险",
          len(r) == 2, f"实际={r!r}")
    check("  第 1 条是敏感词（「诈骗」）",
          len(r) == 2 and "敏感词" in r[0] and "诈骗" in r[0], f"实际={r!r}")
    check("  第 2 条是防诈骗",
          len(r) == 2 and "诈骗" in r[1] and "人工" in r[1], f"实际={r!r}")

    # 五条规则里能同时成立的都触发，用于锁定输出顺序：
    #   敏感词 → 防诈骗 → 物流停滞 → 情绪+异常 → 已取消+退款
    r = _detect_risks({
        "user_message": "我接到电话说快递丢了，你们就是骗子，订单被取消了我要退款",
        "tool_results": [_order("ORD-1001", "已取消"), _logistics("SF1001", "异常")],
    })
    check("四类规则同时命中 → 4 条风险，按固定顺序排列",
          len(r) == 4, f"实际={r!r}")
    check("  顺序[0] = 敏感词", len(r) == 4 and "敏感词" in r[0], f"实际={r!r}")
    check("  顺序[1] = 防诈骗", len(r) == 4 and "诈骗" in r[1] and "敏感词" not in r[1],
          f"实际={r!r}")
    check("  顺序[2] = 情绪 + 物流异常",
          len(r) == 4 and "物流状态异常" in r[2], f"实际={r!r}")
    check("  顺序[3] = 已取消 + 退款",
          len(r) == 4 and "已取消" in r[3], f"实际={r!r}")

    # 健壮性：state 缺字段 / tool_results 元素畸形，都必须不抛异常（否则节点直接 500）
    check("空 state（缺 user_message/tool_results）→ 返回空列表，不抛异常",
          _detect_risks({}) == [])
    check("tool_results 里的 data 是 None → 跳过而不抛异常",
          _detect_risks({"user_message": "急",
                         "tool_results": [{"data": None}]}) == [])
    check("tool_results 里的 data 是字符串 → 跳过而不抛异常",
          _detect_risks({"user_message": "急",
                         "tool_results": [{"data": "oops"}]}) == [])
    check("tool_results 缺 data 键 → 跳过而不抛异常",
          _detect_risks({"user_message": "急",
                         "tool_results": [{"tool_name": "query_logistics"}]}) == [])

    # ── 6. _check_logistics_stale ───────────────────────────────
    section("6) _check_logistics_stale —— 物流停滞（冻结时间，不依赖真实时钟）")

    # 阈值 = FROZEN_NOW - STALE_THRESHOLD_DAYS 天；实现是严格 `updated_dt < threshold`。
    def at(days: int = 0, seconds: int = 0) -> str:
        """FROZEN_NOW 之前 days 天 seconds 秒的时间戳字符串。"""
        return _to_ts(FROZEN_NOW - timedelta(days=days, seconds=seconds))

    # 6.1 正常触发：状态 ∈ {运输中, 异常} 且早于 now-3d
    with _frozen_time(FROZEN_NOW):
        for status in ("运输中", "异常"):
            reason = nodes._check_logistics_stale(
                [_logistics_updated("SF1001", status, at(days=5))])
            check(f"正常触发：status={status} 且 5 天未更新 → 返回非 None",
                  reason is not None, f"实际={reason!r}")
            check(f"  文案含运单号 / 状态 / 天数（{status}）",
                  reason is not None and "SF1001" in reason and status in reason
                  and "已 5 天未更新" in reason, f"实际={reason!r}")

    # 6.2 未超期（负例）
    with _frozen_time(FROZEN_NOW):
        check("未超期：1 天前更新 → 返回 None",
              nodes._check_logistics_stale(
                  [_logistics_updated("SF1001", "异常", at(days=1))]) is None)

    # 6.3 边界：实现用严格 `<`，所以「恰好第 3 天」落在相等侧 → 不触发；
    #     刚过 3 天 1 秒才触发。下面两条把这条分界线钉死。
    with _frozen_time(FROZEN_NOW):
        check("[锁定现状] 边界相等侧：updated == now-3d 恰好第 3 天 → 不触发"
              "（实现是严格 `<`，不是 `<=`）",
              nodes._check_logistics_stale(
                  [_logistics_updated("SF1001", "异常", at(days=3))]) is None)
        just_over = nodes._check_logistics_stale(
            [_logistics_updated("SF1002", "异常", at(days=3, seconds=1))])
        check("[锁定现状] 边界另一侧：刚过第 3 天 1 秒 → 触发，且天数为 3",
              just_over is not None and "已 3 天未更新" in just_over,
              f"实际={just_over!r}")

    # 6.4 状态不匹配：只认「运输中」「异常」，其它状态停滞多久都不触发
    with _frozen_time(FROZEN_NOW):
        for status in ("已签收", "已揽收", "派送中", ""):
            check(f"状态不匹配：status={status!r} 即使 100 天未更新也不触发",
                  nodes._check_logistics_stale(
                      [_logistics_updated("SF1001", status, at(days=100))]) is None)

    # 6.5 updated_at 缺失或为空 → 跳过
    with _frozen_time(FROZEN_NOW):
        check("updated_at 键缺失 → 跳过，不触发",
              nodes._check_logistics_stale(
                  [{"tool_name": "query_logistics",
                    "data": {"tracking_no": "SF1001", "status": "异常"}}]) is None)
        for empty in ("", None):
            check(f"updated_at = {empty!r} → 跳过，不触发",
                  nodes._check_logistics_stale(
                      [_logistics_updated("SF1001", "异常", empty)]) is None)
        # 跳过是 continue 而不是 break/return：前一条没有时间戳，后一条仍要被检查
        skipped_then_hit = nodes._check_logistics_stale([
            {"tool_name": "query_logistics",
             "data": {"tracking_no": "SF0001", "status": "异常"}},
            _logistics_updated("SF0002", "异常", at(days=4)),
        ])
        check("跳过是 continue 而非 break：缺 updated_at 的条目不影响后续条目命中",
              skipped_then_hit is not None and "SF0002" in skipped_then_hit,
              f"实际={skipped_then_hit!r}")

    # 6.6 updated_at 格式不合规 → try/except ValueError → 静默跳过（不抛异常、不打日志）
    with _frozen_time(FROZEN_NOW):
        for label, raw in [
            ("ISO 带 T", "2026-09-10T10:00:00"),
            ("带毫秒", "2026-09-10 10:00:00.123"),
            ("带时区", "2026-09-10T10:00:00+08:00"),
            ("只有日期", "2026-09-10"),
            ("任意乱码", "not-a-date"),
            ("纯空白", "   "),
        ]:
            check(f"[锁定现状] updated_at 格式不合规（{label}）→ 静默跳过、不抛异常",
                  nodes._check_logistics_stale(
                      [_logistics_updated("SF1001", "异常", raw)]) is None,
                  "见「观察记录」第 6 条：数据层改格式会让本规则静默消失")

    # 6.7 tool_results 结构异常 → 跳过而不抛异常
    with _frozen_time(FROZEN_NOW):
        for label, entry in [
            ("data 为 None", {"tool_name": "query_logistics", "data": None}),
            ("data 为字符串", {"tool_name": "query_logistics", "data": "oops"}),
            ("缺 data 键", {"tool_name": "query_logistics"}),
        ]:
            check(f"结构异常（{label}）→ 返回 None 且不抛异常",
                  nodes._check_logistics_stale([entry]) is None)

    # 6.8 多条结果：按遍历顺序取**第一条命中**的
    with _frozen_time(FROZEN_NOW):
        first_hit = nodes._check_logistics_stale([
            _logistics_updated("SF0001", "已签收", at(days=99)),   # 状态不匹配，跳过
            _logistics_updated("SF0002", "异常", at(days=10)),     # 第一条命中
            _logistics_updated("SF0003", "运输中", at(days=10)),   # 也命中，但排在后面
        ])
        check("多条结果：跳过不匹配的，返回第一条命中的 SF0002（遍历顺序确定）",
              first_hit is not None and "SF0002" in first_hit
              and "SF0003" not in first_hit, f"实际={first_hit!r}")

    # 6.9 空列表
    with _frozen_time(FROZEN_NOW):
        check("空 tool_results → None", nodes._check_logistics_stale([]) is None)

    # 6.10 经 _detect_risks 集成：规则 3 是五条里唯一依赖时间的，冻结后同样确定。
    #      消息里不含任何情绪词，确保命中的只能是规则 3 而不是规则 4。
    with _frozen_time(FROZEN_NOW):
        r_stale = _detect_risks({
            "user_message": "帮我看下物流",
            "tool_results": [_logistics_updated("SF1001", "异常", at(days=7))],
        })
        check("集成：经 _detect_risks 也能稳定触发「物流停滞」（五条规则全部可测）",
              len(r_stale) == 1 and "物流停滞" in r_stale[0], f"实际={r_stale!r}")

    # ── 7. 状态机 ───────────────────────────────────────────────
    section("7) can_transition —— 全部合法转换 True / 非法转换 False（排除 1 组未决 Known Issue）")

    check("ALL_TICKET_STATES 恰好 6 个且无重复",
          len(ALL_TICKET_STATES) == 6 and len(set(ALL_TICKET_STATES)) == 6,
          f"实际={ALL_TICKET_STATES!r}")
    check("ALLOWED_TRANSITIONS 的键集合 == ALL_TICKET_STATES（无遗忘的状态）",
          set(ALLOWED_TRANSITIONS) == set(ALL_TICKET_STATES),
          f"键差集={set(ALLOWED_TRANSITIONS) ^ set(ALL_TICKET_STATES)!r}")
    check("所有转换目标都是已知状态（防止改常量时写错字造成死转换）",
          all(t in ALL_TICKET_STATES
              for targets in ALLOWED_TRANSITIONS.values() for t in targets),
          f"越界目标={[t for ts in ALLOWED_TRANSITIONS.values() for t in ts if t not in ALL_TICKET_STATES]!r}")

    for f, t in sorted(LEGAL_TRANSITIONS):
        check(f"合法：{f} → {t} 返回 True", can_transition(f, t) is True)

    check("合法转换共 6 条，与实现一致",
          {(f, t) for f, ts in ALLOWED_TRANSITIONS.items() for t in ts} == LEGAL_TRANSITIONS,
          f"实际={sorted((f, t) for f, ts in ALLOWED_TRANSITIONS.items() for t in ts)!r}")

    # 未决 Known Issue：ALLOWED_TRANSITIONS[待审核] 不含「处理失败」，但 db.review_ticket
    # 在「审批通过但 executor 执行失败」时会把工单**直接 UPDATE 成「处理失败」**，
    # 不走 can_transition。见 E:\AI-Bridge\docs\KnownIssues-customer-service-agent-20260918.md
    # （KI-2026-09-18-01）。
    # 本测试**不对这组转换做正误判定**：既不要求 True 也不要求 False，因此把它从
    # 「非法转换必须 False」的扫描里**排除**——排除的含义是「不判定」，不是「断言它合法」。
    # 该问题定案后，请在此处补一条**新的、明确的**断言（True 或 False 取决于最终选择）。
    KNOWN_ISSUE_TRANSITIONS = {(STATE_PENDING_REVIEW, STATE_FAILED)}

    illegal = [(f, t) for f in ALL_TICKET_STATES for t in ALL_TICKET_STATES
               if (f, t) not in LEGAL_TRANSITIONS
               and (f, t) not in KNOWN_ISSUE_TRANSITIONS]
    bad = [(f, t) for f, t in illegal if can_transition(f, t) is not False]
    check(f"其余 {len(illegal)} 条非法转换全部返回 False"
          f"（已排除 {len(KNOWN_ISSUE_TRANSITIONS)} 组未决 Known Issue，不判定其正误）",
          not bad, f"被误判为合法的：{bad!r}")

    check("不能跳级：新建 → 已解决 必须 False（必须先经处理中）",
          can_transition(STATE_NEW, STATE_RESOLVED) is False)
    check("不能跳级：新建 → 待审核 必须 False",
          can_transition(STATE_NEW, STATE_PENDING_REVIEW) is False)
    check("终态无出边：已解决 → 任何状态均 False",
          all(can_transition(STATE_RESOLVED, t) is False for t in ALL_TICKET_STATES))
    check("终态无出边：已驳回 → 任何状态均 False",
          all(can_transition(STATE_REJECTED, t) is False for t in ALL_TICKET_STATES))
    check("终态无出边：处理失败 → 任何状态均 False",
          all(can_transition(STATE_FAILED, t) is False for t in ALL_TICKET_STATES))
    check("终态的出边集合为空",
          ALLOWED_TRANSITIONS[STATE_RESOLVED] == set()
          and ALLOWED_TRANSITIONS[STATE_REJECTED] == set()
          and ALLOWED_TRANSITIONS[STATE_FAILED] == set())

    # 未知状态必须一律 False（不能因为 .get 默认值写错而变成 True）
    check("未知 from：「不存在的状态」→ False",
          can_transition("不存在的状态", STATE_RESOLVED) is False)
    check("未知 to：新建 → 「不存在的状态」→ False",
          can_transition(STATE_NEW, "不存在的状态") is False)
    check("未知 from + 未知 to → False",
          can_transition("不存在的状态", "不存在的状态") is False)
    check("空字符串状态 → False",
          can_transition("", "") is False and can_transition(STATE_NEW, "") is False)
    check("大小写/空白变体不被当成合法状态",
          can_transition("新建 ", STATE_PROCESSING) is False)
    # 【未决 Known Issue，本测试不做判定】
    # 转换 (待审核 → 处理失败) 目前处于**两处代码互相矛盾**的状态：
    #   - ALLOWED_TRANSITIONS[待审核] = {已解决, 已驳回}，不含「处理失败」
    #     → can_transition(待审核, 处理失败) 返回 False
    #   - db.review_ticket 在「审批通过但 executor 执行失败」时，会把工单从「待审核」
    #     **直接 UPDATE 成「处理失败」**，不走 can_transition
    #     → 这条转换在真实路径上确实会发生
    # 完整证据（含独立 probe 与调用点检索）：
    #   E:\AI-Bridge\docs\KnownIssues-customer-service-agent-20260918.md
    #   条目 KI-2026-09-18-01
    # 因为无法确定「哪一边才是对的」，本测试**既不要求它 True 也不要求它 False**，
    # 上面的非法转换扫描也已把这一组排除。把缺陷断言成 False = 把不一致固化成正确行为。
    # TODO(定案后)：该 Known Issue 解决后，在此处补一条**新的、明确的**断言。
    #   方向 A：把 STATE_FAILED 加进 ALLOWED_TRANSITIONS[待审核] → 断言 True
    #   方向 B：让 review_ticket 失败路径改走 can_transition / 引入独立状态 → 断言 False
    # 在此之前，本文件对该转换保持沉默。

    # ── 汇总 ────────────────────────────────────────────────────
    print()
    print("=" * 74)
    print(f"结果：{passed}/{passed + failed} 通过，失败 {failed}")
    print("=" * 74)
    return 0 if failed == 0 else 1


# ═══════════════════════════════════════════════════════════════════════════
# 观察记录 —— 阅读 nodes._detect_risks / db.can_transition 时发现的行为疑点
#
# 本任务只补测试、不改逻辑，以下问题**一处都没有修改**，仅在此登记。
# 每条都已在上面写了对应用例，将来若决定收紧，测试会先失败、提醒你是有意为之。
# ═══════════════════════════════════════════════════════════════════════════
#
# 1. [情绪词过宽] _LOGISTICS_EMOTION_KEYWORDS 含单字「急」「赔」「不行」，
#    子串匹配下「不着急」「慢慢来不着急」这种**语义相反**的话也会命中情绪信号，
#    进而触发规则 4 转人工。「不行」「说法」也偏口语泛用。
#
# 2. [敏感词过宽] _RISK_KEYWORDS 里的「315」是「12315」的子串；任何含 "315" 的
#    订单号/金额/时间（如 ORD-3150、金额 315）都会被判为敏感词投诉信号。
#    另外「退」「赔」这类单字不在表里，但规则 5 的 refund_words 用了单字。
#
# 3. [诈骗规则过宽] _check_fraud 的 ("电话说","丢") 与 ("短信说","丢") 只要求
#    两个子串各自出现，"丢" 是单字且可出现在任意语境——「客服电话说丢不了的」
#    会被判为遭遇诈骗。相对地，("打电话","快递丢了") 又很具体，两者粒度不一致。
#
# 4. [规则重叠 / 重复计数]「投诉」同时出现在 _RISK_KEYWORDS 和
#    _LOGISTICS_EMOTION_KEYWORDS 里，所以一句含「投诉」的话若同时有物流异常，
#    会产出两条语义重复的风险项，escalate_reason 里也会重复一遍。
#
# 5. [退款诉求过宽] 规则 5 的 refund_words 含单字「退」，于是「退订短信」「退货」
#    这类与退款无关的诉求也会被认定为「已取消 + 要求退款」而转人工。
#    且它只检查消息里有没有「退」，不校验该诉求与 tool_results 里那张已取消订单
#    是否是同一个订单号——多单混在一段对话里时会误判。
#
# 6. [物流停滞规则静默失效] _check_logistics_stale 要求 updated_at 严格匹配
#    "%Y-%m-%d %H:%M:%S"，格式一变（ISO 带 T、带毫秒、带时区）就 continue 跳过，
#    既不抛错也不打日志——数据层改格式会让这条规则**静默消失**。
#    同时它只认状态「运输中」「异常」，其它状态（如「已揽收」）停滞多久都不触发。
#
# 7. [非纯函数] _check_logistics_stale 内部多次调用 datetime.now()，阈值判断与
#    days_stale 计算用的是两个不同的「现在」，跨午夜时描述里的天数可能与触发判断
#    不一致。（生产代码里的这个事实依然成立，**但已不再作为跳过测试的理由**）
#    第 6 节用 patch 把 nodes.datetime 换成 now() 固定的替身，让两次「现在」在测试中
#    必然相同，从而确定性地覆盖了这条规则；因此这个不一致在生产中仍可能存在，
#    测试里则被冻结时间屏蔽掉了，本文件不对它做正误判定。
#
# 8. [状态机与实现不一致] db.review_ticket 在「批准但动作执行失败」时，把工单从
#    「待审核」直接置为「处理失败」，但 ALLOWED_TRANSITIONS[待审核] 只有
#    {已解决, 已驳回}——这条真实发生的转换过不了 can_transition。
#    反过来也说明：状态机的「服务端裁决」目前只是文档承诺，
#    can_transition 在整个仓库里**没有任何生产调用点**（review_ticket 用
#    `UPDATE ... WHERE status='待审核'` 自己做条件校验），
#    decide_node / reply_auto_node 也是直接写 status 字面量。
#    想真正用状态机兜底，得先把这几处写路径收敛到 can_transition 上。
#
# 9. [规则 4 的状态口径不一致] 规则 4 只认 status == "异常"，而规则 3 认
#    「运输中」「异常」。结果：「运输中」且用户情绪激动的单子不触发规则 4，
#    只能指望规则 3 的「停滞 > 3 天」——刚出现问题但客户已经很急的场景会漏转人工。


if __name__ == "__main__":
    sys.exit(main_test())
