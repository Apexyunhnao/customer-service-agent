# 客服工单处理 Agent

[![CI](https://github.com/Apexyunhnao/customer-service-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Apexyunhnao/customer-service-agent/actions/workflows/ci.yml)

> 本服务是**智能客服 Agent 平台**的一部分。统一入口（编排 + 认证）见
> [orchestrator-agent](https://github.com/Apexyunhnao/orchestrator-agent)，
> 政策知识库见 [rag-agent](https://github.com/Apexyunhnao/rag-agent)。
> **平台架构、安全边界、启动与演示步骤、指标口径统一以入口仓库的 README 为准**。

## 功能

基于 LangGraph 的客服工单自动处理系统。接收用户消息 → 意图分类（订单/物流/售后）→
**先只执行查询类工具 → 风险门禁 → 通过才执行写操作**；命中风险的写操作不执行，
落成待审批动作等人工批准。8 个工具覆盖完整售后链路，安全边界写死在工具层。

**业务数据**：SQLite 数据库，20 客户 + 30 订单 + 30 物流记录
**虚实结合**：地址修改和备注是真 SQL 写入，资金相关操作保留 mock 安全边界

## 技术栈

- LangGraph（状态图：classify → handle（查询 → 风险门禁 → 写）→ decide → reply / escalate）
- DeepSeek（意图分类 + 信息提取）
- SQLite（业务库 + 工单库 + 审计表，外键约束，参数化查询）
- FastAPI（HTTP 接口）
- PyJWT + bcrypt（与入口服务共用密钥，**本服务独立验签**）

## 启动

```bash
python -m venv .venv                          # 基准环境 Python 3.11
pip install -r requirements.lock.txt          # 锁文件（含全部传递依赖）
cp .env.example .env                          # 然后填入 AUTH_SECRET（见下）
python data/migrate_to_sqlite.py              # 首次：创建 SQLite 库
uvicorn main:app --port 8001                  # 启动服务
```

> **认证是 fail-closed 的**：`AUTH_SECRET` 没配，服务会**直接拒绝启动**（不再回落到固定默认密钥）。
> 它与 orchestrator-agent / rag-agent 必须是**同一个值**（共享验签）—— 最省事的做法是在
> 入口仓库运行 `start-demo.bat`，它会生成一次随机密钥并写入三仓 `.env`。

> 接口需要认证（HttpOnly Cookie `svc_token`）。**演示请在入口服务
> `http://127.0.0.1:8010/` 登录一次**，Cookie 会自动带到本服务，
> 之后可直接打开 `http://127.0.0.1:8001/ops` 审核工单。

或 Docker：
```bash
docker build -t ticket-agent .
docker run -p 8001:8001 --env-file .env ticket-agent
```

## API

**业务入口**（由编排器调用，需认证）

```json
POST /api/ticket
{"message": "帮我查一下ORD-1003的订单状态", "user_identifier": ""}
→ {"ticket_id": "...", "category": "订单", "status": "待审核", "resolution": "...",
   "proposed_action": {...}, "risk_reasons": [...]}
```

**人工审核接口**（要求 `agent` / `admin` 角色；客户访问返回 403）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tickets?status=待审核` | 工单列表，可按状态筛选 |
| GET | `/api/tickets/{ticket_id}` | 工单详情 + 完整审计时间线 |
| POST | `/api/tickets/{ticket_id}/approve` | 批准：原子更新状态 + 执行待审批动作 + 写审计 |
| POST | `/api/tickets/{ticket_id}/reject` | 驳回：不执行任何动作，只改状态并写审计 |
| GET | `/api/my/tickets` | **客户侧**接口，只返回自己的工单（数据边界） |

**工单状态机**：`新建 → 处理中 → {已解决 | 待审核 → {已解决 | 已驳回} | 处理失败}`。
转换合法性由服务端校验；对非「待审核」状态的工单再次审批返回 **409**（并发下同样只有一个能成功）。

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

## 安全边界（四级防护）

1. **副作用边界（P2 新增）**：风险门禁位于写操作**之前** —— 先只跑查询、判风险、通过才执行写操作。
   命中风险的写操作**根本不执行**，落成带**数据快照**与**规则版本**的 `proposed_action`，等人工批准
2. 工具层硬边界：金额限额写死在函数里（退款 > ¥1000、退差价 > ¥500 直接返回失败），LLM 无法绕过
3. 确定性风险规则：投诉词、防诈骗组合、物流停滞 ≥3 天、已取消订单要求退款 —— 用代码检测，不靠 LLM 判断
4. 人工兜底：解析失败 / 信息不足 → `待审核`；工具执行失败 → `处理失败`

> 为什么第 1 条要单独列：改前的实现是"先执行工具、再判风险"，风险判定只是**事后标记**，
> 写操作早已生效（这个顺序问题是用探针实测出来的，不是看代码猜的）。

## 评估

**测试集**：59 条标注用例 = 47 条开发集 + 12 条留出集（按 8:2 分层切分，seed=42，订单/物流/售后各类都覆盖）。
**数字来源**：`eval/results.json`（由 `eval/run_eval.py --cases all` 自动生成，README 与所有对外表述只引用它，禁止手写）。

| 指标 | 全量（59 条） | 留出集（12 条） |
|------|------------|---------------|
| 分类准确率 | 98.3%（58/59） | **100%**（12/12） |
| 行动准确率 | **100%**（59/59） | **100%**（12/12） |
| 自动处理成功率 | **100%**（25/25） | **100%**（6/6） |
| 工具覆盖准确率 | 92.0%（23/25） | **100%**（6/6） |
| 实际转人工率 | 57.6%（34/59） | 50.0%（6/12） |

口径说明：

- **行动准确率**：**分母 59（全部用例）** —— 判定"自动处理 / 转人工"是否符合标注。实际转人工 34/59 与期望 34/59 **完全吻合**
- **自动处理成功率**：**分母 25**（期望自动处理的用例）—— 这些用例是否都处理成功
- **工具覆盖准确率**：**分母 25**（期望自动处理的用例）—— 期望工具是否都被调用（子集判定，多调不判错）
- **留出集**：从开发阶段起未参与任何 prompt 调整，12 条四项指标全部通过

## 已知限制

- 全量集有 3 条偏差：TC-003 分类标签分歧（问"发货了没"被判订单类、期望物流类，但两个查询都执行了、行动判定正确）、TC-039 选 `process_exchange` 而非 `process_refund`（"退了重新买"本身可两解）、TC-049 只查订单未发起退款（模型判断已超 7 天无理由期）。三条都是**语义歧义 / 业务判断分歧**，不涉及流程缺陷（行动准确率 100%、留出集全通过）
- 资金操作保持 mock（安全设计，非遗漏）
- **审批人无金额分级**：任意 `agent` 可批准任意金额；生产应做"超阈值需 `admin`"
- 退款金额依赖提取：用户未说金额时提取为 `0`（不编造数字），生产应由业务系统带出金额
- 进入 `待审核` 后**无通知**提醒客服
- 无并发处理（单线程 uvicorn）；本服务无独立限流
- 本次改造前的历史工单**没有审计记录**（`ticket_audit` 表为本次新建）
- 订单/物流/客户数据为模拟数据；业务规则含"物流停滞 ≥3 天转人工"这类**时间敏感**判断，因此 `data/migrate_to_sqlite.py` 会把 mock 数据的时间戳平移到当前时间，保证评估可跨时间复现

## 健康检查

GET /health → `{"status":"ok","service":"ticket-agent"}`
