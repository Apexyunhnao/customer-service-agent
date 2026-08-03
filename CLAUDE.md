# CLAUDE.md — 客服工单处理 Agent

## 技术栈
- Python 3.10+
- LangGraph（状态图编排）
- langchain-openai（DeepSeek OpenAI 兼容接口）
- FastAPI（API 服务）
- SQLite（本地数据存储）

## 目录结构
```
.
├── main.py          # FastAPI 入口，路由定义
├── graph.py         # LangGraph 状态图构建
├── nodes.py         # 图节点实现（分类、处理、转人工等）
├── tools.py         # Agent 可调用的工具函数
├── state.py         # TypedDict 状态定义
├── db.py            # SQLite 数据库操作
├── eval/            # 评估脚本与测试用例
├── data/            # mock 数据
├── docs/            # 文档
├── logs/            # 运行日志
└── requirements.txt
```

## 代码风格
- 所有注释、docstring 使用中文
- 所有函数、方法标注类型（type hints）
- 命名：文件/模块用小写下划线，类用 PascalCase，函数/变量用 snake_case
- 每个文件职责单一，一次只改一个文件，不跨文件大改

## 工作原则
- 简单优先，不过度设计
- 改完代码自动检查语法（`python -m py_compile`）
- mock 数据优先，不连真实业务系统
