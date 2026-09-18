"""Advisor 模式独立入口（Agent Integration 第一阶段）。

只加载 `advisor_mode`（只读 + 只建议）；不 import 原 demo 的 db/tools/nodes/graph。
原 demo 入口 main.py 保持不动（仍在 8001 跑它自带的 mock 库）。

启动：.venv/Scripts/python.exe -m uvicorn advisor_main:app --port 8003
"""
import os

from advisor_mode import app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("ADVISOR_PORT", "8003")))
