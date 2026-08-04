# 客服工单处理 Agent

基于 LangGraph + DeepSeek 的智能客服工单处理系统，自动分类、执行、回复常见工单类型，复杂情况自动转人工。

## 架构

```
用户消息 → classify（意图分类）→ handle（工具调用）→ decide（决策）
                                                      ├─ auto → reply_auto（生成回复）
                                                      └─ escalate → escalate（转人工）
```

## 目录结构

```
.
├── main.py              # FastAPI 服务入口（POST /api/ticket, GET /）
├── graph.py             # LangGraph 状态图 + run_ticket 封装
├── nodes.py             # 5 个节点函数 + 风险信号检测
├── tools.py             # 8 个工具函数（安全边界写死）
├── state.py             # TypedDict 状态定义
├── db.py                # SQLite 工单存储
├── logger.py            # 结构化 JSON 日志
├── demo.py              # Demo 脚本（前 3 条测试用例）
├── eval/
│   ├── test_cases.json  # 56 条标注测试集
│   ├── run_eval.py      # 评估脚本（含重试）
│   ├── results.json     # 最新评估结果
│   └── failure_analysis.md
├── data/
│   ├── mock_db.json     # Mock 数据（30 条订单 + 物流 + 客户）
│   └── tickets.db       # SQLite 工单记录
├── docs/
│   ├── requirements.md  # 需求文档
│   ├── tech_selection.md# 技术选型文档
│   └── development-log.md
├── logs/
│   └── agent.log        # 结构化日志（每单一条 JSON）
├── requirements.txt
└── .env                 # DEEPSEEK_API_KEY=your-key
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
echo "DEEPSEEK_API_KEY=your-key" > .env

# 3. 跑 Demo（前 3 条测试用例）
python demo.py

# 4. 启动 API 服务
uvicorn main:app --host 127.0.0.1 --port 8001

# 5. 跑完整评估
python eval/run_eval.py
```

## API

**POST /api/ticket**

```json
// 请求
{"message": "帮我查一下ORD-1003的订单状态"}

// 响应
{
  "ticket_id": "API-20260804-abc123",
  "category": "订单",
  "status": "已解决",
  "resolution": "您好！已为您查到订单 ORD-1003 的最新情况...",
  "escalate_reason": ""
}
```

## 评估结果

56 条标注测试集按 8:2 切分：44 条训练集（开发迭代用）+ 12 条留出集（holdout，最终验收用）。

| 轮次 | 改动 | 分类准确率 | 行动准确率 | 失败数 |
|------|------|-----------|-----------|--------|
| 1 | 初版 | 69.6% | 73.2% | 27 |
| 2 | +标注修正+重试 | 96.4% | 82.1% | 22 |
| 3 | +风险检测+先查后写 | 98.2% | 87.5% | 8 |
| 4 | +防诈骗+物流细化 | 98.2% | 92.9% | 5 |
| 5 | +停滞检测 | 98.2% | 89.3% | 7 |
| 6 | +数据对齐+规则归位 | 100.0% | 100.0% | 0 |
| 7 | +留出集+运行时重试+history+硬校验 | train 95.5% | train 100% | 2* |

**留出集验收（当前切分下未参与开发调试的 12 条用例）**：分类 100% / 行动 100% / 工具覆盖率 100% / 自动处理成功率 100%。

*注 1：train 2 条失败为分类争议（退差价归订单/售后、快递丢归订单/物流），行动均正确转人工；第 8-9 轮整改后已全部校准，当前 --all 59 条全 100%、0 失败。
*注 2：工具覆盖率只在 auto 用例统计，escalate 用例不评判工具调用（tools_ok 为空，不计入分母）。

最新评估结果详见 [eval/results.json](eval/results.json)（--all 全量 59 条），留出集（holdout）结果见 [docs/development-log.md](docs/development-log.md) 第 7 轮。

## 技术栈

| 层面 | 选型 |
|------|------|
| Agent 编排 | LangGraph (StateGraph) |
| LLM | DeepSeek v4-pro（OpenAI 兼容接口） |
| Web 框架 | FastAPI + uvicorn |
| 数据存储 | SQLite |
| 日志 | Python logging + JSON 格式 |
| 环境管理 | python-dotenv |

选型理由详见 [docs/tech_selection.md](docs/tech_selection.md)。

## 文档

- [需求文档](docs/requirements.md)
- [技术选型](docs/tech_selection.md)
- [开发日志](docs/development-log.md)
