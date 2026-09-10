# 客服工单处理 Agent

## 功能

基于 LangGraph 的客服工单自动处理系统。接收用户消息 → 意图分类（订单/物流/售后）→ 调用对应工具 → 自动处理或转人工。8 个工具覆盖完整售后链路，安全边界写死在工具层。

**业务数据**：SQLite 数据库，20 客户 + 30 订单 + 30 物流记录
**虚实结合**：地址修改和备注是真 SQL 写入，资金相关操作保留 mock 安全边界

## 技术栈

- LangGraph（状态图：classify→handle→decide→auto/escalate→reply）
- DeepSeek（意图分类 + 信息提取）
- SQLite（业务数据库，外键约束，参数化查询）
- FastAPI（HTTP 接口）

## 启动

```bash
pip install -r requirements.txt
python data/migrate_to_sqlite.py    # 首次：创建 SQLite 库
uvicorn main:app --port 8001        # 启动服务
```

或 Docker：
```bash
docker build -t ticket-agent .
docker run -p 8001:8001 --env-file .env ticket-agent
```

## API

POST /api/ticket
```json
{"message": "帮我查一下ORD-1003的订单状态", "user_identifier": ""}
→ {"ticket_id": "...", "category": "订单", "status": "已解决", "resolution": "..."}
```

## 工具列表

| 工具 | 功能 | 写操作 |
|------|------|--------|
| query_order | 查询订单状态 | 读 |
| query_logistics | 查询物流轨迹 | 读 |
| update_address | 修改收货地址 | ✅ SQL UPDATE |
| update_remark | 添加订单备注 | ✅ SQL INSERT |
| refund_price_diff | 退差价（限额 ¥500） | mock |
| process_refund | 处理退款（限额 ¥1000） | mock |
| urge_delivery | 催派送 | mock |
| process_exchange | 处理换货 | mock |

## 安全边界（三级防护）

1. 工具层硬边界：金额限额写死在函数（`if amount > 1000: return success=False`），LLM 无法绕过
2. 确定性风险规则：投诉词、已取消/已签收状态用代码检测，不靠 LLM 判断
3. 人工兜底：解析失败、信息不足 → 转人工

## 评估

**测试集**：59 条标注用例 = 47 条开发集 + 12 条留出集（按 8:2 分层切分，seed=42，订单/物流/售后各类都覆盖）。
**数字来源**：`eval/results.json`（由 `eval/run_eval.py --cases all` 自动生成，README 与简历只引用它，禁止手写）。

| 指标 | 全量（59 条） | 留出集（12 条） |
|------|------------|---------------|
| 分类准确率 | 98.3%（58/59） | **100%**（12/12） |
| 行动准确率 | **100%**（59/59） | **100%**（12/12） |
| 自动处理成功率 | **100%**（25/25） | **100%**（6/6） |
| 工具覆盖准确率 | 92.0%（23/25） | **100%**（6/6） |
| 实际转人工率 | 57.6%（34/59） | 50.0%（6/12） |

口径说明：

- **行动准确率 / 自动处理成功率**：分母 25 条"期望自动处理"用例。实际转人工率与期望值**完全吻合**（34/59 vs 期望 34/59）
- **工具覆盖准确率**：期望工具集合是否都被调用（子集判定，多调不判错），分母 25 条自动处理用例
- **留出集**：从开发阶段起未参与任何 prompt 调整，12 条四项指标全部通过

## 已知限制

- 全量集有 3 条偏差：TC-003 分类标签分歧（问"发货了没"被判订单类、期望物流类，但两个查询都执行了、行动判定正确）、TC-039 选 `process_exchange` 而非 `process_refund`（"退了重新买"本身可两解）、TC-049 只查订单未发起退款（模型判断已超 7 天无理由期）。三条都是**语义歧义 / 业务判断分歧**，不涉及流程缺陷（行动准确率 100%、留出集全通过）
- 资金操作保持 mock（安全设计，非遗漏）
- 无并发处理、无用户认证
- 订单/物流/客户数据为模拟数据；业务规则含"物流停滞 ≥3 天转人工"这类**时间敏感**判断，因此 `data/migrate_to_sqlite.py` 会把 mock 数据的时间戳平移到当前时间，保证评估可跨时间复现

## 健康检查

GET /health → `{"status":"ok","service":"ticket-agent"}`
