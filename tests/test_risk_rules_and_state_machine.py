"""风控规则与工单状态机回归测试 —— 锁定现有确定性规则的行为，不改任何生产代码。

被测对象（全部是纯函数 / 纯常量，不触发 LLM、不连数据库、不发网络请求、不写文件）：

    nodes.py  _RISK_KEYWORDS / _LOGISTICS_EMOTION_KEYWORDS
              _has_emotion(msg) -> bool            物流情绪词检测
              _check_fraud(msg) -> str | None      防诈骗组合关键词检测
              _detect_risks(state) -> list[str]    风险总入口，五条规则按序执行
    db.py     can_transition(from, to) -> bool
              ALLOWED_TRANSITIONS / ALL_TICKET_STATES / STATE_*

为什么需要这个文件：这五条风险规则是**写操作之前的唯一门禁**（命中即拦下退款/改地址等副作用，
转人工审批），规则表被改宽或改窄都意味着安全边界位移，但没有任何测试盯住它。
本文件用「正例 + 负例 + 边界」把当前行为钉死：将来谁改动了关键词或状态机，
这里必须先失败，改动才是有意识的。

运行方式（不需要 pytest，也不需要 DEEPSEEK_API_KEY）：

    python tests/test_risk_rules_and_state_machine.py     # 退出码 0=全过 / 1=有失败

"""  # noqa: D400
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    """造一条物流查询结果；**不带 updated_at** → _check_logistics_stale 会跳过，
    这样本文件的断言就不会被「当前时间」污染（保持确定性、可重复）。"""
    return {"tool_name": "query_logistics",
            "data": {"tracking_no": tracking_no, "status": status}}


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

    # ── 6. 状态机 ───────────────────────────────────────────────
    section("6) can_transition —— 全部合法转换 True / 全部非法转换 False")

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

    illegal = [(f, t) for f in ALL_TICKET_STATES for t in ALL_TICKET_STATES
               if (f, t) not in LEGAL_TRANSITIONS]
    bad = [(f, t) for f, t in illegal if can_transition(f, t) is not False]
    check(f"其余 {len(illegal)} 条非法转换全部返回 False",
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
    check("[锁定现状] 待审核 → 处理失败 返回 False（但 review_ticket 实际会这么写）",
          can_transition(STATE_PENDING_REVIEW, STATE_FAILED) is False,
          "见「观察记录」第 8 条：状态机常量与审批实现不一致")

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
#    不一致。本测试因此刻意不构造超期数据，只测其余四条规则。
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
