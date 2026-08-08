"""
FastAPI 服务 — 客服工单处理 Agent 的 HTTP 接口。

启动方式：
    uvicorn main:app --host 127.0.0.1 --port 8001

接口：
    GET  /            — 简易 Web 页面（内嵌 HTML）
    POST /api/ticket  — 提交工单，返回处理结果
"""

import os
import uuid
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()  # 从项目根目录 .env 加载 DEEPSEEK_API_KEY

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from graph import run_ticket

app = FastAPI(title="客服工单处理 Agent")


# ── 数据模型 ────────────────────────────────────────────────────

class TicketRequest(BaseModel):
    message: str
    user_identifier: str = ""  # 可选，用户手机号或标识


class TicketResponse(BaseModel):
    ticket_id: str
    category: str
    status: str
    resolution: str
    escalate_reason: str


# ── API ─────────────────────────────────────────────────────────

@app.post("/api/ticket", response_model=TicketResponse)
def handle_ticket(req: TicketRequest) -> TicketResponse:
    """接收用户消息，跑完整流水线，返回处理结果。"""
    ticket_id = f"API-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

    from db import get_user_history, create_ticket
    history = get_user_history(req.user_identifier) if req.user_identifier else []

    state = {
        "ticket_id": ticket_id,
        "user_message": req.message,
        "user_identifier": req.user_identifier,
        "category": "",
        "extracted_info": {},
        "tool_results": [],
        "resolution": "",
        "status": "新建",
        "escalate_reason": "",
        "history": history,
    }

    result = run_ticket(state)

    # 工单落库
    create_ticket(
        ticket_id=ticket_id,
        user_message=req.message,
        user_identifier=req.user_identifier,
        category=result.get("category", ""),
        status=result.get("status", "已关闭"),
        resolution=result.get("resolution", ""),
        escalate_reason=result.get("escalate_reason", ""),
    )

    return TicketResponse(
        ticket_id=ticket_id,
        category=result.get("category", ""),
        status=result.get("status", ""),
        resolution=result.get("resolution", ""),
        escalate_reason=result.get("escalate_reason", ""),
    )


@app.get("/health")
def health() -> dict:
    """健康检查。"""
    return {"healthy": True, "service": "customer-service-agent"}


# ── Web 页面 ────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>客服工单处理 Agent</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f5f5; color: #333; }
  .container { max-width: 720px; margin: 40px auto; padding: 24px; }
  h1 { font-size: 24px; margin-bottom: 8px; }
  .subtitle { color: #999; font-size: 14px; margin-bottom: 24px; }
  .card { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06); margin-bottom: 20px; }
  textarea { width: 100%; height: 100px; border: 1px solid #ddd; border-radius: 8px; padding: 12px; font-size: 15px; resize: vertical; font-family: inherit; }
  textarea:focus { outline: none; border-color: #4a90d9; box-shadow: 0 0 0 3px rgba(74,144,217,.15); }
  button { background: #4a90d9; color: #fff; border: none; border-radius: 8px; padding: 10px 28px; font-size: 15px; cursor: pointer; margin-top: 12px; }
  button:hover { background: #3a7bc8; }
  button:disabled { background: #bbb; cursor: not-allowed; }
  .result { display: none; }
  .result.show { display: block; }
  .row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #f0f0f0; }
  .row:last-child { border-bottom: none; }
  .label { color: #888; font-size: 14px; }
  .value { font-weight: 500; font-size: 14px; max-width: 420px; text-align: right; word-break: break-all; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
  .tag-auto { background: #e6f7e6; color: #389e0d; }
  .tag-escalate { background: #fff2e8; color: #d46b08; }
  .loading { color: #999; font-size: 14px; margin-top: 8px; display: none; }
  .loading.show { display: block; }
</style>
</head>
<body>
<div class="container">
  <h1>客服工单处理 Agent</h1>
  <p class="subtitle">输入用户消息，Agent 自动分类、执行操作并返回结果</p>

  <div class="card">
    <textarea id="message" placeholder="输入用户消息，例如：帮我查一下 ORD-1003 的订单状态"></textarea>
    <div style="display:flex;align-items:center;gap:12px">
      <button id="submitBtn" onclick="submitTicket()">提交工单</button>
      <span class="loading" id="loading">处理中...</span>
    </div>
  </div>

  <div class="card result" id="resultCard">
    <h2 style="font-size:18px;margin-bottom:16px">处理结果</h2>
    <div class="row"><span class="label">工单 ID</span><span class="value" id="rTicketId">-</span></div>
    <div class="row"><span class="label">分类</span><span class="value" id="rCategory">-</span></div>
    <div class="row"><span class="label">状态</span><span class="value" id="rStatus">-</span></div>
    <div class="row"><span class="label">操作</span><span class="value"><span class="tag" id="rAction">-</span></span></div>
    <div class="row"><span class="label">回复 / 说明</span><span class="value" id="rResolution">-</span></div>
    <div class="row" id="escalateRow" style="display:none"><span class="label">转人工原因</span><span class="value" id="rEscalate">-</span></div>
  </div>
</div>

<script>
async function submitTicket() {
  const msg = document.getElementById('message').value.trim();
  if (!msg) return;

  const btn = document.getElementById('submitBtn');
  const loading = document.getElementById('loading');
  const resultCard = document.getElementById('resultCard');
  btn.disabled = true;
  loading.classList.add('show');
  resultCard.classList.remove('show');

  try {
    const resp = await fetch('/api/ticket', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg})
    });
    const data = await resp.json();

    document.getElementById('rTicketId').textContent = data.ticket_id;
    document.getElementById('rCategory').textContent = data.category;
    document.getElementById('rStatus').textContent = data.status;

    const tag = document.getElementById('rAction');
    if (data.escalate_reason) {
      tag.textContent = '转人工';
      tag.className = 'tag tag-escalate';
      document.getElementById('escalateRow').style.display = 'flex';
      document.getElementById('rEscalate').textContent = data.escalate_reason;
    } else {
      tag.textContent = '自动处理';
      tag.className = 'tag tag-auto';
      document.getElementById('escalateRow').style.display = 'none';
    }

    document.getElementById('rResolution').textContent = data.resolution || '-';
    resultCard.classList.add('show');
  } catch (e) {
    alert('请求失败: ' + e.message);
  } finally {
    btn.disabled = false;
    loading.classList.remove('show');
  }
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """返回简易 Web 页面。"""
    return HTML_PAGE
