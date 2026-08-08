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

| 指标 | 全量（59条） | 留出集（12条） |
|------|------------|---------------|
| 分类准确率 | 100% | 100% |
| 行动准确率 | 96.6% | 100% |
| 自动处理成功率 | 92.0% | — |

## 已知限制

- 3 条失败案例全部是 LLM 工具选择偏差（退差价金额提取、历史推理），与数据库无关
- 资金操作保持 mock（安全设计，非遗漏）
- 无并发处理、无用户认证
- 订单/物流/客户数据为模拟数据

## 健康检查

GET /health → `{"status":"ok","service":"ticket-agent"}`
