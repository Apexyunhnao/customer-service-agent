"""
FastAPI 服务 — 客服工单处理 Agent 的 HTTP 接口。

启动方式：
    uvicorn main:app --host 127.0.0.1 --port 8001

接口：
    GET  /            — 简易 Web 页面（内嵌 HTML）
    POST /api/ticket  — 提交工单，返回处理结果
"""

import os
import time
import uuid
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()  # 从项目根目录 .env 加载 DEEPSEEK_API_KEY

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from auth import current_user, require_roles
from graph import run_ticket, run_ticket_with_steps

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
    steps: list[dict] = []      # 执行链路：分类→工具执行→风险决策→自动回复/转人工
    elapsed_ms: int = 0
    trace_id: str = ""          # 跨服务关联标识（调用方传入或本服务生成）


# ── API ─────────────────────────────────────────────────────────

@app.post("/api/ticket", response_model=TicketResponse)
def handle_ticket(req: TicketRequest, request: Request,
                  user: dict = Depends(current_user)) -> TicketResponse:
    """接收用户消息，跑完整流水线，返回处理结果。

    身份由**本服务自己验签**（Bearer 优先，其次 Cookie）—— 不信任编排层传来的角色字符串。
    """
    # 关联标识：优先沿用调用方（编排器）传入的 trace_id，没有则自己生成
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    # 工单归属（数据边界的关键）：**只接受已验签 JWT 里的 owner_id**。
    #
    # 历史教训：此前实现是 `headers.get("X-Owner-Id") or user["owner_id"] or body.user_identifier`，
    # 请求头优先级最高 —— 任何持有合法 JWT 的调用方（哪怕只是个普通客户账号），
    # 只要在请求里加一个 X-Owner-Id 头，就能以他人身份建单、读取他人历史（已实测复现）。
    # 请求头与请求体都是调用方可控的输入，不能作为身份依据；身份只能来自验签结果。
    owner_id = user.get("owner_id", "")
    print(f"[main] 工单请求 by {user.get('username')}({user.get('role')}) owner={owner_id} trace={trace_id}")
    ticket_id = f"API-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

    from db import get_user_history, create_ticket
    history = get_user_history(owner_id) if owner_id else []

    state = {
        "ticket_id": ticket_id,
        "trace_id": trace_id,
        "user_message": req.message,
        "user_identifier": owner_id,
        "category": "",
        "extracted_info": {},
        "tool_results": [],
        "resolution": "",
        "status": "新建",
        "escalate_reason": "",
        "history": history,
    }

    t0 = time.time()
    result, steps = run_ticket_with_steps(state)
    elapsed_ms = int((time.time() - t0) * 1000)

    # 工单落库（含执行链路、关联标识、待审批动作）
    create_ticket(
        ticket_id=ticket_id,
        user_message=req.message,
        user_identifier=owner_id,
        category=result.get("category", ""),
        status=result.get("status", "已解决"),
        resolution=result.get("resolution", ""),
        escalate_reason=result.get("escalate_reason", ""),
        steps=steps,
        trace_id=trace_id,
        proposed_action=result.get("proposed_action") or None,
        risk_reasons=result.get("risk_reasons") or None,
    )

    return TicketResponse(
        ticket_id=ticket_id,
        category=result.get("category", ""),
        status=result.get("status", ""),
        resolution=result.get("resolution", ""),
        escalate_reason=result.get("escalate_reason", ""),
        steps=steps,
        elapsed_ms=elapsed_ms,
        trace_id=trace_id,
    )


# ── 人工审核接口（P2 人工审核闭环）───────────────────────────────

class ReviewRequest(BaseModel):
    note: str = ""


@app.get("/api/tickets")
def api_ticket_list(status: str = "",
                    _user: dict = Depends(require_roles("agent", "admin"))) -> dict:
    """工单列表（客服/管理员）。可按状态筛选，例如 `?status=待审核`。"""
    from db import ALL_TICKET_STATES, list_tickets

    items = list_tickets(status=status)
    return {"count": len(items), "tickets": items, "states": ALL_TICKET_STATES}


@app.get("/api/tickets/{ticket_id}")
def api_ticket_detail(ticket_id: str,
                      _user: dict = Depends(require_roles("agent", "admin"))) -> dict:
    """工单详情 + 完整审计时间线（客服/管理员）。"""
    from db import get_ticket, list_audit

    t = get_ticket(ticket_id)
    if t is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return {"ticket": t, "audit": list_audit(ticket_id)}


@app.post("/api/tickets/{ticket_id}/approve")
def api_ticket_approve(ticket_id: str, req: ReviewRequest, request: Request,
                       user: dict = Depends(require_roles("agent", "admin"))) -> dict:
    """批准：原子条件更新 → 同事务执行 proposed_action → 写审计。

    - 只有「待审核」状态可批准，否则 409；
    - 并发重复提交只有一个能成功，另一个 409（不会重复执行）；
    - 执行失败则整单转「处理失败」（不会假装成功）。
    """
    from db import review_ticket
    from nodes import execute_proposed_action

    approval_trace = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    r = review_ticket(
        ticket_id, approve=True,
        actor_id=user.get("username", ""), actor_role=user.get("role", ""),
        note=req.note, approval_trace_id=approval_trace,
        executor=execute_proposed_action,
    )
    if not r.get("success"):
        http_map = {"NOT_FOUND": 404, "INVALID_STATE": 409, "CONFLICT": 409, "EXEC_FAILED": 500}
        raise HTTPException(status_code=http_map.get(r.get("code", ""), 400),
                            detail=r.get("message", "审批失败"))
    print(f"[review] 批准 {ticket_id} by {user.get('username')} trace={approval_trace}")
    return r


@app.post("/api/tickets/{ticket_id}/reject")
def api_ticket_reject(ticket_id: str, req: ReviewRequest, request: Request,
                      user: dict = Depends(require_roles("agent", "admin"))) -> dict:
    """驳回：不执行任何业务动作，只改状态并写审计。"""
    from db import review_ticket

    approval_trace = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    r = review_ticket(
        ticket_id, approve=False,
        actor_id=user.get("username", ""), actor_role=user.get("role", ""),
        note=req.note, approval_trace_id=approval_trace,
    )
    if not r.get("success"):
        http_map = {"NOT_FOUND": 404, "INVALID_STATE": 409, "CONFLICT": 409}
        raise HTTPException(status_code=http_map.get(r.get("code", ""), 400),
                            detail=r.get("message", "驳回失败"))
    print(f"[review] 驳回 {ticket_id} by {user.get('username')} trace={approval_trace}")
    return r


@app.get("/api/my/tickets")
def api_my_tickets(user: dict = Depends(current_user)) -> dict:
    """客户自己的工单 —— 数据边界：只返回属于当前登录用户的工单，不看别人的。"""
    from db import list_tickets

    items = list_tickets(owner_id=user.get("owner_id", ""))
    return {"count": len(items), "tickets": items,
            "owner_id": user.get("owner_id", ""), "role": user.get("role", "")}


@app.get("/health")
def health() -> dict:
    """健康检查。"""
    return {"healthy": True, "service": "customer-service-agent"}


@app.get("/stats")
def stats(_user: dict = Depends(require_roles("agent", "admin"))) -> dict:
    """工单统计 + 最近工单（含执行链路），供观测页使用。仅客服/管理员可看。"""
    from db import list_recent_tickets, ticket_stats

    st = ticket_stats()
    return {
        **st,
        "recent": list_recent_tickets(30),
    }


# ── Web 页面 ────────────────────────────────────────────────────

_USER_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>提交工单 · 客服工单处理 Agent</title>
<style>
  :root {
    --bg:#F5F7FC; --surface:#FFFFFF; --sunken:#F7F9FD;
    --border:#E3E7F2; --border-strong:#CBD3E4; --hair:#EFF2F8;
    --text:#0F1520; --text-2:#3A4560; --text-3:#7A8BA0;
    --accent:#2563EB; --accent-hover:#1D4ED8; --accent-soft:#EEF3FF;
    --ok:#15803D; --ok-bg:#E7F6EC; --warn:#9A6700; --warn-bg:#FFF8E1;
    --err:#C1272D; --err-bg:#FDEBEC;
    --mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Courier New", monospace;
    --s1:8px; --s2:12px; --s3:16px; --s4:24px; --s5:32px; --s6:48px;
    --radius:12px; --radius-sm:8px;
    --shadow-sm: 0 1px 2px rgba(20,45,110,.06), 0 0 0 1px rgba(20,45,110,.05);
    --shadow-md: 0 2px 4px rgba(20,45,110,.05), 0 8px 24px rgba(20,45,110,.10), 0 0 0 1px rgba(20,45,110,.06);
    --ease: cubic-bezier(.22,1,.36,1);
    --t-fast:140ms; --t-base:220ms; --t-slow:380ms;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html, body { height:100%; }
  body {
    display:flex; overflow:hidden;
    background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI Variable Display","Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
    font-size:14.5px; line-height:1.6; -webkit-font-smoothing:antialiased;
  }
  .sidebar {
    width:248px; flex:none; background:var(--surface);
    border-right:1px solid var(--border);
    display:flex; flex-direction:column; padding:var(--s4) var(--s3); gap:var(--s5);
  }
  .brand { display:flex; align-items:center; gap:10px; padding:0 6px; }
  .brand-logo {
    width:32px; height:32px; flex:none; border-radius:9px;
    background:linear-gradient(135deg,#2563EB,#60A5FA); color:#fff;
    display:flex; align-items:center; justify-content:center;
    font-weight:800; font-size:14px; box-shadow:0 2px 8px rgba(37,99,235,.32);
  }
  .brand-name { font-size:13.5px; font-weight:700; letter-spacing:-.01em; line-height:1.3; }
  .brand-sub { font-size:11px; color:var(--text-3); }
  .nav { display:flex; flex-direction:column; gap:2px; }
  .nav-label {
    font-size:10.5px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
    color:var(--text-3); padding:0 8px; margin-bottom:6px;
  }
  .nav-item {
    display:flex; align-items:center; gap:10px;
    padding:9px 10px; border-radius:var(--radius-sm);
    color:var(--text-2); text-decoration:none; font-size:13.5px; font-weight:500;
    transition:background var(--t-fast) var(--ease), color var(--t-fast) var(--ease);
  }
  .nav-item:hover { background:var(--sunken); color:var(--text); }
  .nav-item.active { background:var(--accent-soft); color:var(--accent); font-weight:650; }
  .nav-item .ico { width:17px; text-align:center; font-size:13.5px; flex:none; opacity:.9; }
  .sidebar-foot { margin-top:auto; padding:0 8px; font-size:11px; color:var(--text-3); line-height:1.9; }
  .dot-ok { display:inline-block; width:6px; height:6px; border-radius:50%; background:#22C55E; margin-right:5px; vertical-align:1px; }

  .main { flex:1; min-width:0; display:flex; flex-direction:column; }
  .topbar {
    height:58px; flex:none; border-bottom:1px solid var(--border);
    background:rgba(255,255,255,.78); backdrop-filter:blur(10px);
    display:flex; align-items:center; justify-content:space-between; gap:var(--s3);
    padding:0 var(--s5);
  }
  .topbar h1 { font-size:15.5px; font-weight:700; letter-spacing:-.01em; }
  .topbar .sub { font-size:12.5px; color:var(--text-3); }
  .content { flex:1; overflow-y:auto; padding:var(--s5); }

  .btn-primary {
    padding:11px 26px; font-size:14.5px; font-family:inherit; font-weight:600;
    color:#fff; border:0; border-radius:var(--radius-sm); cursor:pointer; white-space:nowrap;
    background:linear-gradient(180deg,#3B76F0,#2563EB);
    box-shadow:0 1px 2px rgba(37,99,235,.30), inset 0 1px 0 rgba(255,255,255,.22);
    transition:filter var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
  }
  .btn-primary:hover:not(:disabled) { filter:brightness(1.07); }
  .btn-primary:active:not(:disabled) { transform:translateY(1px) scale(.995); box-shadow:inset 0 1px 3px rgba(0,0,0,.16); }
  .btn-primary:disabled { opacity:.45; cursor:not-allowed; }
  .chip {
    font-size:12.5px; font-family:inherit; color:var(--accent);
    background:var(--accent-soft); border:0; box-shadow:inset 0 0 0 1px rgba(37,99,235,.15);
    border-radius:999px; padding:5px 13px; cursor:pointer; text-align:left;
    transition:background var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease), transform var(--t-fast) var(--ease);
  }
  .chip:hover { background:#E3ECFF; box-shadow:inset 0 0 0 1px rgba(37,99,235,.28); }
  .chip:active { transform:scale(.97); }
  .badge { display:inline-flex; align-items:center; gap:4px; font-size:11.5px; font-weight:650; padding:2px 9px; border-radius:999px; }
  .badge-ok { color:var(--ok); background:var(--ok-bg); box-shadow:inset 0 0 0 1px rgba(21,128,61,.16); }
  .badge-warn { color:var(--warn); background:var(--warn-bg); box-shadow:inset 0 0 0 1px rgba(154,103,0,.16); }
  .badge-plain { color:var(--text-2); background:#F1F4FA; box-shadow:inset 0 0 0 1px var(--border); }
  .tag-tool {
    font-family:var(--mono); font-size:12px; background:#F1F4FA;
    box-shadow:inset 0 0 0 1px var(--border); border-radius:6px; padding:2px 8px; color:var(--text-2);
  }
  .arrow { color:var(--text-3); margin:0 5px; }
  .panel { background:var(--surface); border-radius:var(--radius); box-shadow:var(--shadow-sm); overflow:hidden; }
  .panel-head {
    padding:12px var(--s3); border-bottom:1px solid var(--hair);
    font-size:11px; font-weight:700; letter-spacing:.07em; text-transform:uppercase; color:var(--text-3);
  }
  .empty { color:var(--text-3); font-size:13px; padding:var(--s4); text-align:center; }

  /* ══ 客户端：全宽对话（行业做法：不用左右气泡）══ */
  .thread { max-width:768px; margin:0 auto; }
  .turn { padding:var(--s4) 0; border-top:1px solid var(--hair); animation:rise var(--t-slow) var(--ease) both; }
  .turn:first-child { border-top:0; padding-top:0; }
  .turn-head { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
  .turn-badge {
    width:24px; height:24px; flex:none; border-radius:7px;
    display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:700;
  }
  .turn.bot .turn-badge { background:linear-gradient(135deg,#2563EB,#60A5FA); color:#fff; box-shadow:0 2px 6px rgba(37,99,235,.26); }
  .turn.user .turn-badge { background:#E6ECF7; color:var(--text-2); }
  .turn-name { font-size:13px; font-weight:650; }
  .turn-time { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .turn-body { font-size:14.5px; line-height:1.72; white-space:pre-wrap; word-break:break-word; }
  .turn.user .turn-body { color:var(--text-2); }
  .turn.bot .turn-body.typing { color:var(--text-3); }
  .turn-meta { margin-top:10px; font-size:11.5px; color:var(--text-3); }
  .turn-meta a { color:var(--accent); text-decoration:none; font-weight:600; }
  .turn-meta a:hover { text-decoration:underline; }
  .composer {
    flex:none; border-top:1px solid var(--border);
    background:rgba(255,255,255,.86); backdrop-filter:blur(10px);
    padding:var(--s3) var(--s5) var(--s4);
  }
  .composer-inner { max-width:768px; margin:0 auto; }
  .composer-row { display:flex; gap:var(--s2); }
  .composer input[type=text] {
    flex:1; min-width:0; padding:12px 15px; font-size:14.5px; font-family:inherit;
    color:var(--text); background:#fff; border:0; border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border-strong); outline:none;
    transition:box-shadow var(--t-fast) var(--ease);
  }
  .composer input[type=text]:focus { box-shadow:inset 0 0 0 1px var(--accent), 0 0 0 4px rgba(37,99,235,.13); }
  .composer-hint { display:flex; flex-wrap:wrap; gap:var(--s1); align-items:center; margin-top:var(--s2); }
  .composer-hint .lbl { font-size:12px; color:var(--text-3); }

  /* ══ 服务端：观测台 ══ */
  .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:var(--s3); margin-bottom:var(--s3); }
  .metric { background:var(--surface); border-radius:var(--radius); padding:var(--s3) var(--s4); box-shadow:var(--shadow-sm); }
  .metric .lbl { font-size:11.5px; color:var(--text-3); margin-bottom:5px; font-weight:600; }
  .metric .val { font-size:25px; font-weight:750; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
  .metric .val.small { font-size:15px; font-weight:650; padding-top:6px; }
  .ops { display:grid; grid-template-columns:minmax(260px,340px) minmax(0,1fr); gap:var(--s3); align-items:start; }
  .list { max-height:calc(100vh - 300px); overflow-y:auto; }
  .row {
    padding:11px var(--s3); border-bottom:1px solid var(--hair); cursor:pointer;
    transition:background var(--t-fast) var(--ease);
  }
  .row:last-child { border-bottom:0; }
  .row:hover { background:var(--sunken); }
  .row.sel { background:var(--accent-soft); }
  .row-title { font-size:13px; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row-meta { font-size:11px; color:var(--text-3); margin-top:3px; font-variant-numeric:tabular-nums; }
  .turn-card { border-bottom:1px solid var(--hair); }
  .turn-card:last-child { border-bottom:0; }
  .turn-card > summary {
    list-style:none; cursor:pointer; padding:var(--s3); display:block;
    transition:background var(--t-fast) var(--ease);
  }
  .turn-card > summary::-webkit-details-marker { display:none; }
  .turn-card > summary:hover { background:var(--sunken); }
  .turn-q { font-size:13.5px; font-weight:600; margin-bottom:5px; }
  .turn-sub { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .steps { padding:0 var(--s3) var(--s3); }
  .step { position:relative; padding-left:30px; padding-bottom:var(--s3); }
  .step:last-child { padding-bottom:2px; }
  .step::before { content:""; position:absolute; left:9px; top:24px; bottom:-2px; width:1px; background:var(--border); }
  .step:last-child::before { display:none; }
  .step-no {
    position:absolute; left:0; top:3px; width:20px; height:20px; border-radius:50%;
    background:var(--accent-soft); color:var(--accent); box-shadow:inset 0 0 0 1px rgba(37,99,235,.18);
    font-size:11px; font-weight:700; line-height:20px; text-align:center;
  }
  .step-head { display:flex; align-items:baseline; gap:var(--s2); flex-wrap:wrap; }
  .step-node { font-size:13.5px; font-weight:650; }
  .step-ms { font-size:12px; color:var(--accent); font-variant-numeric:tabular-nums; }
  .step-total { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .step-body { margin-top:var(--s2); display:flex; flex-direction:column; gap:var(--s2); }
  .item {
    background:var(--sunken); border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border); padding:var(--s2) var(--s3); font-size:12.5px;
  }
  .item-role { display:inline-block; font-size:11px; font-weight:700; color:var(--text-3); letter-spacing:.04em; margin-bottom:5px; }
  .call { display:flex; flex-direction:column; gap:4px; margin:4px 0; }
  .call-name { font-family:var(--mono); font-size:12.5px; font-weight:650; color:var(--accent); }
  .call-args {
    font-family:var(--mono); font-size:11.5px; color:var(--text-2); background:#fff;
    border-radius:6px; box-shadow:inset 0 0 0 1px var(--border);
    padding:6px 9px; white-space:pre-wrap; word-break:break-all;
  }
  .item-text {
    font-size:12.5px; color:var(--text-2); line-height:1.65; white-space:pre-wrap; word-break:break-word;
    max-height:160px; overflow-y:auto;
  }

  @keyframes rise { from { opacity:0; transform:translateY(7px); } to { opacity:1; transform:none; } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation:none !important; transition:none !important; }
  }
  @media (max-width: 900px) {
    .sidebar { width:64px; padding:var(--s3) 10px; }
    .brand-text, .nav-item span:not(.ico), .nav-label, .sidebar-foot, .topbar .sub { display:none; }
    .ops { grid-template-columns:1fr; }
    .content { padding:var(--s3); }
    .topbar { padding:0 var(--s3); }
    .composer { padding:var(--s3); }
  }
  @media (max-width: 560px) {
    body { font-size:14px; }
    .composer-row { flex-direction:column; }
    .btn-primary { width:100%; }
    .composer-hint { flex-direction:column; align-items:stretch; }
  }

  /* ══ p2 专用：工单表单与结果卡 ══ */
  .form-wrap { max-width:768px; margin:0 auto; }
  textarea {
    width:100%; min-height:104px; resize:vertical; font-family:inherit; font-size:14.5px;
    color:var(--text); background:#fff; border:0; border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border-strong); outline:none; padding:13px 15px; line-height:1.6;
    transition:box-shadow var(--t-fast) var(--ease);
  }
  textarea:focus { box-shadow:inset 0 0 0 1px var(--accent), 0 0 0 4px rgba(37,99,235,.13); }
  .form-foot { display:flex; align-items:center; gap:var(--s2); margin-top:var(--s2); flex-wrap:wrap; }
  .field { display:flex; gap:var(--s3); padding:9px 0; font-size:13.5px; align-items:baseline; border-bottom:1px solid var(--hair); }
  .field:last-child { border-bottom:0; }
  .field .k { color:var(--text-3); flex:none; width:92px; }
  .field .v { color:var(--text); word-break:break-word; white-space:pre-wrap; }
  .op-auto { color:var(--ok); background:var(--ok-bg); box-shadow:inset 0 0 0 1px rgba(21,128,61,.16); }
  .op-esc  { color:var(--warn); background:var(--warn-bg); box-shadow:inset 0 0 0 1px rgba(154,103,0,.16); }
</style>
</head>
<body>
<aside class="sidebar">
  <div class="brand">
    <div class="brand-logo">T</div>
    <div class="brand-text">
      <div class="brand-name">客服工单 Agent</div>
      <div class="brand-sub">Ticket Pipeline</div>
    </div>
  </div>
  <nav class="nav">
    <div class="nav-label">工作台</div>
    <a class="nav-item active" href="/"><span class="ico">🎫</span><span>提交工单</span></a>
    <a class="nav-item" href="/ops"><span class="ico">📊</span><span>运行观测</span></a>
  </nav>
  <div class="sidebar-foot">
    <div><span class="dot-ok"></span>服务正常</div>
    <div>build v2 · 应用壳</div>
  </div>
</aside>
<div class="main">
  <div class="topbar">
    <h1>提交工单</h1>
    <div class="sub">客服工单流水线 · 分类 → 提取 → 风险 → 工具 → 回复</div>
  </div>
  <div class="content" id="scroll">
    <div class="form-wrap">
      <div class="form-foot" style="margin-top:0;margin-bottom:var(--s3)">
        <span style="font-size:12.5px;color:var(--text-3)">试试：</span>
        <button class="chip" onclick="fill(0)">查订单状态</button>
        <button class="chip" onclick="fill(1)">改收货地址</button>
        <button class="chip" onclick="fill(2)">翻新机投诉（触发转人工）</button>
      </div>

      <textarea id="msg" placeholder="描述用户的问题，例如：帮我查一下 ORD-1003 的订单状态"></textarea>
      <div class="form-foot">
        <button id="submitBtn" class="btn-primary" onclick="submit()">提交工单</button>
        <span id="hint" style="font-size:12.5px;color:var(--text-3)">流水线：意图分类 → 信息提取 → 风险决策 → 工具执行 → 自动回复 / 转人工</span>
      </div>
    </div>

    <div class="form-wrap" id="resultWrap" style="margin-top:var(--s4);display:none">
      <div class="panel" style="margin-bottom:var(--s3)">
        <div class="panel-head">处理结果</div>
        <div style="padding:var(--s3)">
          <div class="field"><span class="k">工单 ID</span><span class="v" id="rId"></span></div>
          <div class="field"><span class="k">意图分类</span><span class="v" id="rCat"></span></div>
          <div class="field"><span class="k">工单状态</span><span class="v" id="rStatus"></span></div>
          <div class="field"><span class="k">处理动作</span><span class="v" id="rAction"></span></div>
          <div class="field"><span class="k">回复内容</span><span class="v" id="rRes"></span></div>
          <div class="field" id="escRow" style="display:none"><span class="k">转人工原因</span><span class="v" id="rEsc"></span></div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head">执行链路（点开看每跳）</div>
        <div id="steps"></div>
      </div>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const LS_KEY = "p2_last_tickets";
const PRESETS = [
  "帮我查一下 ORD-1003 的订单状态",
  "帮我把 ORD-1004 的收货地址改成深圳市南山区科技园1号",
  "ORD-1002 买的戴森吸尘器疑似翻新机，全是划痕，要求退款并赔偿"
];
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function fill(i) { $("msg").value = PRESETS[i]; $("msg").focus(); }

const NODE_LABEL = {
  classify: "意图分类", handle: "工具执行", decide: "风险决策",
  reply_auto: "自动回复", escalate: "转人工"
};

function renderItem(it) {
  const label = it.name ? esc(it.name) : "";
  if (it.type === "ToolResult") {
    const badge = it.ok
      ? '<span class="badge badge-ok">成功</span>'
      : '<span class="badge badge-warn">失败</span>';
    return '<div class="item"><div class="item-role">' + label + " " + badge + '</div>' +
      '<div class="item-text">' + esc(it.content) + '</div></div>';
  }
  if (it.type === "StateField") {
    return '<div class="item"><div class="item-role">' + label + '</div>' +
      '<div class="item-text">' + esc(it.content) + '</div></div>';
  }
  return '<div class="item"><div class="item-text">' + esc(it.content || "") + '</div></div>';
}

function renderSteps(steps) {
  if (!steps || !steps.length) {
    $("steps").innerHTML = '<div class="empty">本次没有链路记录</div>';
    return;
  }
  $("steps").innerHTML = '<div class="steps">' + steps.map((s, i) =>
    '<div class="step">' +
      '<span class="step-no">' + (i + 1) + '</span>' +
      '<div class="step-head">' +
        '<span class="step-node">' + esc(NODE_LABEL[s.node] || s.node) + '</span>' +
        '<span class="step-ms">' + (s.step_ms / 1000).toFixed(1) + 's</span>' +
        '<span class="step-total">累计 ' + (s.total_ms / 1000).toFixed(1) + 's</span>' +
      '</div>' +
      '<div class="step-body">' + (s.items || []).map(renderItem).join("") + '</div>' +
    '</div>'
  ).join("") + '</div>';
}

async function submit() {
  const msg = $("msg").value.trim();
  if (!msg) { $("msg").focus(); return; }
  $("submitBtn").disabled = true;
  $("hint").textContent = "处理中…（分类 / 提取 / 风险判定 / 工具执行，约 5–15 秒）";
  try {
    const resp = await fetch("/api/ticket", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, user_identifier: "" })
    });
    const d = await resp.json();
    if (!resp.ok) { $("hint").textContent = "处理失败：" + ((d && d.detail) || ("HTTP " + resp.status)); return; }

    $("resultWrap").style.display = "block";
    $("rId").textContent = d.ticket_id || "-";
    $("rCat").textContent = d.category || "-";
    $("rStatus").textContent = d.status || "-";
    const esc2 = d.escalate_reason || "";
    $("rAction").innerHTML = esc2
      ? '<span class="badge op-esc">转人工</span>'
      : '<span class="badge op-auto">自动处理</span>';
    $("rRes").textContent = d.resolution || "-";
    if (esc2) { $("escRow").style.display = "flex"; $("rEsc").textContent = esc2; }
    else { $("escRow").style.display = "none"; }
    renderSteps(d.steps);
    $("hint").textContent = "完成 · " + (d.elapsed_ms / 1000).toFixed(1) + "s · " +
      ((d.steps || []).length) + " 跳链路";
  } catch (e) {
    $("hint").textContent = "网络或服务异常：" + (e && e.message ? e.message : e);
  } finally {
    $("submitBtn").disabled = false;
  }
}
$("msg").addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
});
</script>
</body>
</html>"""


_OPS_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>运行观测 · 客服工单处理 Agent</title>
<style>
  :root {
    --bg:#F5F7FC; --surface:#FFFFFF; --sunken:#F7F9FD;
    --border:#E3E7F2; --border-strong:#CBD3E4; --hair:#EFF2F8;
    --text:#0F1520; --text-2:#3A4560; --text-3:#7A8BA0;
    --accent:#2563EB; --accent-hover:#1D4ED8; --accent-soft:#EEF3FF;
    --ok:#15803D; --ok-bg:#E7F6EC; --warn:#9A6700; --warn-bg:#FFF8E1;
    --err:#C1272D; --err-bg:#FDEBEC;
    --mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Courier New", monospace;
    --s1:8px; --s2:12px; --s3:16px; --s4:24px; --s5:32px; --s6:48px;
    --radius:12px; --radius-sm:8px;
    --shadow-sm: 0 1px 2px rgba(20,45,110,.06), 0 0 0 1px rgba(20,45,110,.05);
    --shadow-md: 0 2px 4px rgba(20,45,110,.05), 0 8px 24px rgba(20,45,110,.10), 0 0 0 1px rgba(20,45,110,.06);
    --ease: cubic-bezier(.22,1,.36,1);
    --t-fast:140ms; --t-base:220ms; --t-slow:380ms;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html, body { height:100%; }
  body {
    display:flex; overflow:hidden;
    background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI Variable Display","Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
    font-size:14.5px; line-height:1.6; -webkit-font-smoothing:antialiased;
  }
  .sidebar {
    width:248px; flex:none; background:var(--surface);
    border-right:1px solid var(--border);
    display:flex; flex-direction:column; padding:var(--s4) var(--s3); gap:var(--s5);
  }
  .brand { display:flex; align-items:center; gap:10px; padding:0 6px; }
  .brand-logo {
    width:32px; height:32px; flex:none; border-radius:9px;
    background:linear-gradient(135deg,#2563EB,#60A5FA); color:#fff;
    display:flex; align-items:center; justify-content:center;
    font-weight:800; font-size:14px; box-shadow:0 2px 8px rgba(37,99,235,.32);
  }
  .brand-name { font-size:13.5px; font-weight:700; letter-spacing:-.01em; line-height:1.3; }
  .brand-sub { font-size:11px; color:var(--text-3); }
  .nav { display:flex; flex-direction:column; gap:2px; }
  .nav-label {
    font-size:10.5px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
    color:var(--text-3); padding:0 8px; margin-bottom:6px;
  }
  .nav-item {
    display:flex; align-items:center; gap:10px;
    padding:9px 10px; border-radius:var(--radius-sm);
    color:var(--text-2); text-decoration:none; font-size:13.5px; font-weight:500;
    transition:background var(--t-fast) var(--ease), color var(--t-fast) var(--ease);
  }
  .nav-item:hover { background:var(--sunken); color:var(--text); }
  .nav-item.active { background:var(--accent-soft); color:var(--accent); font-weight:650; }
  .nav-item .ico { width:17px; text-align:center; font-size:13.5px; flex:none; opacity:.9; }
  .sidebar-foot { margin-top:auto; padding:0 8px; font-size:11px; color:var(--text-3); line-height:1.9; }
  .dot-ok { display:inline-block; width:6px; height:6px; border-radius:50%; background:#22C55E; margin-right:5px; vertical-align:1px; }

  .main { flex:1; min-width:0; display:flex; flex-direction:column; }
  .topbar {
    height:58px; flex:none; border-bottom:1px solid var(--border);
    background:rgba(255,255,255,.78); backdrop-filter:blur(10px);
    display:flex; align-items:center; justify-content:space-between; gap:var(--s3);
    padding:0 var(--s5);
  }
  .topbar h1 { font-size:15.5px; font-weight:700; letter-spacing:-.01em; }
  .topbar .sub { font-size:12.5px; color:var(--text-3); }
  .content { flex:1; overflow-y:auto; padding:var(--s5); }

  .btn-primary {
    padding:11px 26px; font-size:14.5px; font-family:inherit; font-weight:600;
    color:#fff; border:0; border-radius:var(--radius-sm); cursor:pointer; white-space:nowrap;
    background:linear-gradient(180deg,#3B76F0,#2563EB);
    box-shadow:0 1px 2px rgba(37,99,235,.30), inset 0 1px 0 rgba(255,255,255,.22);
    transition:filter var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
  }
  .btn-primary:hover:not(:disabled) { filter:brightness(1.07); }
  .btn-primary:active:not(:disabled) { transform:translateY(1px) scale(.995); box-shadow:inset 0 1px 3px rgba(0,0,0,.16); }
  .btn-primary:disabled { opacity:.45; cursor:not-allowed; }
  .chip {
    font-size:12.5px; font-family:inherit; color:var(--accent);
    background:var(--accent-soft); border:0; box-shadow:inset 0 0 0 1px rgba(37,99,235,.15);
    border-radius:999px; padding:5px 13px; cursor:pointer; text-align:left;
    transition:background var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease), transform var(--t-fast) var(--ease);
  }
  .chip:hover { background:#E3ECFF; box-shadow:inset 0 0 0 1px rgba(37,99,235,.28); }
  .chip:active { transform:scale(.97); }
  .badge { display:inline-flex; align-items:center; gap:4px; font-size:11.5px; font-weight:650; padding:2px 9px; border-radius:999px; }
  .badge-ok { color:var(--ok); background:var(--ok-bg); box-shadow:inset 0 0 0 1px rgba(21,128,61,.16); }
  .badge-warn { color:var(--warn); background:var(--warn-bg); box-shadow:inset 0 0 0 1px rgba(154,103,0,.16); }
  .badge-plain { color:var(--text-2); background:#F1F4FA; box-shadow:inset 0 0 0 1px var(--border); }
  .tag-tool {
    font-family:var(--mono); font-size:12px; background:#F1F4FA;
    box-shadow:inset 0 0 0 1px var(--border); border-radius:6px; padding:2px 8px; color:var(--text-2);
  }
  .arrow { color:var(--text-3); margin:0 5px; }
  .panel { background:var(--surface); border-radius:var(--radius); box-shadow:var(--shadow-sm); overflow:hidden; }
  .panel-head {
    padding:12px var(--s3); border-bottom:1px solid var(--hair);
    font-size:11px; font-weight:700; letter-spacing:.07em; text-transform:uppercase; color:var(--text-3);
  }
  .empty { color:var(--text-3); font-size:13px; padding:var(--s4); text-align:center; }

  /* ══ 客户端：全宽对话（行业做法：不用左右气泡）══ */
  .thread { max-width:768px; margin:0 auto; }
  .turn { padding:var(--s4) 0; border-top:1px solid var(--hair); animation:rise var(--t-slow) var(--ease) both; }
  .turn:first-child { border-top:0; padding-top:0; }
  .turn-head { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
  .turn-badge {
    width:24px; height:24px; flex:none; border-radius:7px;
    display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:700;
  }
  .turn.bot .turn-badge { background:linear-gradient(135deg,#2563EB,#60A5FA); color:#fff; box-shadow:0 2px 6px rgba(37,99,235,.26); }
  .turn.user .turn-badge { background:#E6ECF7; color:var(--text-2); }
  .turn-name { font-size:13px; font-weight:650; }
  .turn-time { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .turn-body { font-size:14.5px; line-height:1.72; white-space:pre-wrap; word-break:break-word; }
  .turn.user .turn-body { color:var(--text-2); }
  .turn.bot .turn-body.typing { color:var(--text-3); }
  .turn-meta { margin-top:10px; font-size:11.5px; color:var(--text-3); }
  .turn-meta a { color:var(--accent); text-decoration:none; font-weight:600; }
  .turn-meta a:hover { text-decoration:underline; }
  .composer {
    flex:none; border-top:1px solid var(--border);
    background:rgba(255,255,255,.86); backdrop-filter:blur(10px);
    padding:var(--s3) var(--s5) var(--s4);
  }
  .composer-inner { max-width:768px; margin:0 auto; }
  .composer-row { display:flex; gap:var(--s2); }
  .composer input[type=text] {
    flex:1; min-width:0; padding:12px 15px; font-size:14.5px; font-family:inherit;
    color:var(--text); background:#fff; border:0; border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border-strong); outline:none;
    transition:box-shadow var(--t-fast) var(--ease);
  }
  .composer input[type=text]:focus { box-shadow:inset 0 0 0 1px var(--accent), 0 0 0 4px rgba(37,99,235,.13); }
  .composer-hint { display:flex; flex-wrap:wrap; gap:var(--s1); align-items:center; margin-top:var(--s2); }
  .composer-hint .lbl { font-size:12px; color:var(--text-3); }

  /* ══ 服务端：观测台 ══ */
  .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:var(--s3); margin-bottom:var(--s3); }
  .metric { background:var(--surface); border-radius:var(--radius); padding:var(--s3) var(--s4); box-shadow:var(--shadow-sm); }
  .metric .lbl { font-size:11.5px; color:var(--text-3); margin-bottom:5px; font-weight:600; }
  .metric .val { font-size:25px; font-weight:750; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
  .metric .val.small { font-size:15px; font-weight:650; padding-top:6px; }
  .ops { display:grid; grid-template-columns:minmax(260px,340px) minmax(0,1fr); gap:var(--s3); align-items:start; }
  .list { max-height:calc(100vh - 300px); overflow-y:auto; }
  .row {
    padding:11px var(--s3); border-bottom:1px solid var(--hair); cursor:pointer;
    transition:background var(--t-fast) var(--ease);
  }
  .row:last-child { border-bottom:0; }
  .row:hover { background:var(--sunken); }
  .row.sel { background:var(--accent-soft); }
  .row-title { font-size:13px; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row-meta { font-size:11px; color:var(--text-3); margin-top:3px; font-variant-numeric:tabular-nums; }
  .turn-card { border-bottom:1px solid var(--hair); }
  .turn-card:last-child { border-bottom:0; }
  .turn-card > summary {
    list-style:none; cursor:pointer; padding:var(--s3); display:block;
    transition:background var(--t-fast) var(--ease);
  }
  .turn-card > summary::-webkit-details-marker { display:none; }
  .turn-card > summary:hover { background:var(--sunken); }
  .turn-q { font-size:13.5px; font-weight:600; margin-bottom:5px; }
  .turn-sub { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .steps { padding:0 var(--s3) var(--s3); }
  .step { position:relative; padding-left:30px; padding-bottom:var(--s3); }
  .step:last-child { padding-bottom:2px; }
  .step::before { content:""; position:absolute; left:9px; top:24px; bottom:-2px; width:1px; background:var(--border); }
  .step:last-child::before { display:none; }
  .step-no {
    position:absolute; left:0; top:3px; width:20px; height:20px; border-radius:50%;
    background:var(--accent-soft); color:var(--accent); box-shadow:inset 0 0 0 1px rgba(37,99,235,.18);
    font-size:11px; font-weight:700; line-height:20px; text-align:center;
  }
  .step-head { display:flex; align-items:baseline; gap:var(--s2); flex-wrap:wrap; }
  .step-node { font-size:13.5px; font-weight:650; }
  .step-ms { font-size:12px; color:var(--accent); font-variant-numeric:tabular-nums; }
  .step-total { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .step-body { margin-top:var(--s2); display:flex; flex-direction:column; gap:var(--s2); }
  .item {
    background:var(--sunken); border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border); padding:var(--s2) var(--s3); font-size:12.5px;
  }
  .item-role { display:inline-block; font-size:11px; font-weight:700; color:var(--text-3); letter-spacing:.04em; margin-bottom:5px; }
  .call { display:flex; flex-direction:column; gap:4px; margin:4px 0; }
  .call-name { font-family:var(--mono); font-size:12.5px; font-weight:650; color:var(--accent); }
  .call-args {
    font-family:var(--mono); font-size:11.5px; color:var(--text-2); background:#fff;
    border-radius:6px; box-shadow:inset 0 0 0 1px var(--border);
    padding:6px 9px; white-space:pre-wrap; word-break:break-all;
  }
  .item-text {
    font-size:12.5px; color:var(--text-2); line-height:1.65; white-space:pre-wrap; word-break:break-word;
    max-height:160px; overflow-y:auto;
  }

  @keyframes rise { from { opacity:0; transform:translateY(7px); } to { opacity:1; transform:none; } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation:none !important; transition:none !important; }
  }
  @media (max-width: 900px) {
    .sidebar { width:64px; padding:var(--s3) 10px; }
    .brand-text, .nav-item span:not(.ico), .nav-label, .sidebar-foot, .topbar .sub { display:none; }
    .ops { grid-template-columns:1fr; }
    .content { padding:var(--s3); }
    .topbar { padding:0 var(--s3); }
    .composer { padding:var(--s3); }
  }
  @media (max-width: 560px) {
    body { font-size:14px; }
    .composer-row { flex-direction:column; }
    .btn-primary { width:100%; }
    .composer-hint { flex-direction:column; align-items:stretch; }
  }

  .field { display:flex; gap:var(--s3); padding:9px 0; font-size:13.5px; align-items:baseline; border-bottom:1px solid var(--hair); }
  .field:last-child { border-bottom:0; }
  .field .k { color:var(--text-3); flex:none; width:92px; }
  .field .v { color:var(--text); word-break:break-word; white-space:pre-wrap; }
  .op-auto { color:var(--ok); background:var(--ok-bg); box-shadow:inset 0 0 0 1px rgba(21,128,61,.16); }
  .op-esc  { color:var(--warn); background:var(--warn-bg); box-shadow:inset 0 0 0 1px rgba(154,103,0,.16); }
</style>
</head>
<body>
<aside class="sidebar">
  <div class="brand">
    <div class="brand-logo">T</div>
    <div class="brand-text">
      <div class="brand-name">客服工单 Agent</div>
      <div class="brand-sub">Ticket Pipeline</div>
    </div>
  </div>
  <nav class="nav">
    <div class="nav-label">工作台</div>
    <a class="nav-item" href="/"><span class="ico">🎫</span><span>提交工单</span></a>
    <a class="nav-item active" href="/ops"><span class="ico">📊</span><span>运行观测</span></a>
  </nav>
  <div class="sidebar-foot">
    <div><span class="dot-ok"></span>服务正常</div>
    <div>build v2 · 应用壳</div>
  </div>
</aside>
<div class="main">
  <div class="topbar">
    <h1>运行观测</h1>
    <div class="sub">工单流水线 · 分类 / 风险 / 工具 / 链路</div>
  </div>
  <div class="content">
    <div class="metrics" id="metrics"></div>
    <div class="ops">
      <div class="panel">
        <div class="panel-head">工单记录</div>
        <div class="list" id="list"><div class="empty">加载中…</div></div>
      </div>
      <div class="panel">
        <div class="panel-head">工单详情 · 执行链路</div>
        <div id="detail"><div class="empty">从左侧选择一条工单，查看它的处理链路</div></div>
      </div>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
const NODE_LABEL = {
  classify: "意图分类", handle: "工具执行", decide: "风险决策",
  reply_auto: "自动回复", escalate: "转人工"
};
let STATS = null, SEL = null;
let METRIC_SIG = "", LIST_SIG = "", DETAIL_SIG = "";

function sigList(d) { return JSON.stringify((d.recent || []).map(r => [r.id, r.status])); }
function sigDetail(d) {
  const r = (d.recent || []).find(x => x.id === SEL);
  return String(SEL) + "|" + (r ? r.status + "|" + ((r.steps || []).length) : "none");
}

function renderMetrics() {
  const s = STATS || {};
  const cats = s.by_category || {};
  const catStr = Object.keys(cats).length
    ? Object.entries(cats).map(([k, v]) => k + " ×" + v).join("　") : "—";
  const total = s.total || 0;
  const escN = s.escalated || 0;
  const rate = total ? ((escN / total) * 100).toFixed(0) + "%" : "—";
  const items = [
    { lbl: "工单总数", val: total },
    { lbl: "自动处理", val: (s.auto || 0) },
    { lbl: "转人工", val: escN + (total ? "　(" + rate + ")" : "") },
    { lbl: "分类分布", val: catStr, small: true }
  ];
  $("metrics").innerHTML = items.map(i =>
    '<div class="metric"><div class="lbl">' + esc(i.lbl) + '</div>' +
    '<div class="val' + (i.small ? " small" : "") + '">' + esc(i.val) + '</div></div>').join("");
}

function renderList() {
  const list = (STATS && STATS.recent) || [];
  if (!list.length) {
    $("list").innerHTML = '<div class="empty">还没有工单<br/>去「提交工单」跑一条就会有</div>';
    return;
  }
  $("list").innerHTML = list.map(r =>
    '<div class="row' + (r.id === SEL ? " sel" : "") + '" data-id="' + r.id + '">' +
      '<div class="row-title">' + esc(r.user_message) + '</div>' +
      '<div class="row-meta">' + esc(r.category || "未分类") + '　·　' +
        (r.escalate_reason ? "转人工" : "自动处理") + '　·　' +
        ((r.steps || []).length) + ' 跳　·　' + esc(r.created_at) + '</div>' +
    '</div>').join("");
  Array.from($("list").children).forEach(el => {
    el.onclick = () => {
      SEL = parseInt(el.dataset.id, 10);
      DETAIL_SIG = sigDetail(STATS);
      renderList(); renderDetail();
    };
  });
}

function renderItem(it) {
  const label = it.name ? esc(it.name) : "";
  if (it.type === "ToolResult") {
    const badge = it.ok
      ? '<span class="badge badge-ok">成功</span>'
      : '<span class="badge badge-warn">失败</span>';
    return '<div class="item"><div class="item-role">' + label + " " + badge + '</div>' +
      '<div class="item-text">' + esc(it.content) + '</div></div>';
  }
  if (it.type === "StateField") {
    return '<div class="item"><div class="item-role">' + label + '</div>' +
      '<div class="item-text">' + esc(it.content) + '</div></div>';
  }
  return '<div class="item"><div class="item-text">' + esc(it.content || "") + '</div></div>';
}

function renderDetail() {
  const list = (STATS && STATS.recent) || [];
  const rec = list.find(r => r.id === SEL);
  if (!rec) { $("detail").innerHTML = '<div class="empty">从左侧选择一条工单，查看它的处理链路</div>'; return; }
  const escReason = rec.escalate_reason || "";
  const head =
    '<div style="padding:var(--s3);border-bottom:1px solid var(--hair)">' +
      '<div style="font-size:13.5px;font-weight:600;margin-bottom:8px">' + esc(rec.user_message) + '</div>' +
      '<div class="field"><span class="k">工单 ID</span><span class="v">' + esc(rec.ticket_id) + '</span></div>' +
      '<div class="field"><span class="k">意图分类</span><span class="v">' + esc(rec.category || "-") + '</span></div>' +
      '<div class="field"><span class="k">状态</span><span class="v">' + esc(rec.status || "-") + '</span></div>' +
      '<div class="field"><span class="k">处理动作</span><span class="v">' +
        (escReason ? '<span class="badge op-esc">转人工</span>' : '<span class="badge op-auto">自动处理</span>') +
      '</span></div>' +
      (escReason ? '<div class="field"><span class="k">转人工原因</span><span class="v">' + esc(escReason) + '</span></div>' : '') +
    '</div>';
  // P2：待审核工单 → 显示「待审批动作」（尚未执行）+ 批准/驳回
  const proposed = rec.proposed_action || {};
  const actions = proposed.actions || [];
  const risks = rec.risk_reasons || [];
  let reviewBlock = "";
  if (rec.status === "待审核") {
    const actHtml = actions.length
      ? actions.map(a => esc(a.tool_name) + " " + esc(JSON.stringify(a.arguments || {}))).join("<br>")
      : "（本单没有写操作，仅需人工核实）";
    reviewBlock =
      '<div style="padding:12px 16px;border-bottom:1px solid var(--hair);background:var(--warn-bg)">' +
        '<div style="font-size:12px;font-weight:700;color:var(--warn);margin-bottom:8px">⚠ 待人工审核 —— 以下动作尚未执行</div>' +
        '<div class="field"><span class="k">待审批动作</span><span class="v">' + actHtml + '</span></div>' +
        (risks.length ? '<div class="field"><span class="k">风险依据</span><span class="v">' + esc(risks.join("；")) + '</span></div>' : '') +
        (proposed.rule_version ? '<div class="field"><span class="k">规则版本</span><span class="v">' + esc(proposed.rule_version) + '</span></div>' : '') +
        '<div style="margin-top:10px;display:flex;gap:8px;align-items:center">' +
          '<input id="noteInput" placeholder="备注 / 驳回原因（可空）" ' +
            'style="flex:1;padding:7px 10px;border:0;border-radius:7px;box-shadow:inset 0 0 0 1px var(--border-strong);font-family:inherit;font-size:13px;outline:none">' +
          '<button onclick="doReview(true)" style="padding:7px 16px;border:0;border-radius:7px;background:var(--ok);color:#fff;font-weight:650;font-size:13px;cursor:pointer;font-family:inherit">批准并执行</button>' +
          '<button onclick="doReview(false)" style="padding:7px 16px;border:0;border-radius:7px;background:var(--err);color:#fff;font-weight:650;font-size:13px;cursor:pointer;font-family:inherit">驳回</button>' +
        '</div>' +
      '</div>';
  } else if (rec.resolution) {
    reviewBlock =
      '<div style="padding:12px 16px;border-bottom:1px solid var(--hair)">' +
        '<div class="field"><span class="k">处理结果</span><span class="v" style="white-space:pre-wrap">' +
          esc(rec.resolution.slice(0, 800)) + '</span></div>' +
      '</div>';
  }

  const steps = rec.steps || [];
  const body = steps.length
    ? '<div class="steps">' + steps.map((s, i) =>
        '<div class="step">' +
          '<span class="step-no">' + (i + 1) + '</span>' +
          '<div class="step-head">' +
            '<span class="step-node">' + esc(NODE_LABEL[s.node] || s.node) + '</span>' +
            '<span class="step-ms">' + (s.step_ms / 1000).toFixed(1) + 's</span>' +
            '<span class="step-total">累计 ' + (s.total_ms / 1000).toFixed(1) + 's</span>' +
          '</div>' +
          '<div class="step-body">' + (s.items || []).map(renderItem).join("") + '</div>' +
        '</div>').join("") + '</div>'
    : '<div class="empty">这条工单没有链路数据（本次改造前的历史记录）</div>';
  $("detail").innerHTML = head + reviewBlock +
    '<div style="padding:12px 16px;border-bottom:1px solid var(--hair)">' +
      '<div style="font-size:11.5px;font-weight:700;color:var(--text-3);margin-bottom:8px">审计时间线</div>' +
      '<div id="auditBox"><div class="empty">加载中…</div></div>' +
    '</div>' + body;
  renderAudit(rec.ticket_id);
}

// ── P2：审计时间线 & 人工审批动作 ─────────────────────────────
let AUDIT = [];

async function renderAudit(ticketId) {
  if (!$("auditBox")) return;
  try {
    const r = await fetch("/api/tickets/" + encodeURIComponent(ticketId));
    if (!r.ok) { if ($("auditBox")) $("auditBox").innerHTML = '<div class="empty">无权限或不存在</div>'; return; }
    const d = await r.json();
    AUDIT = d.audit || [];
  } catch (e) { AUDIT = []; }
  if (!$("auditBox")) return;   // 期间用户已切换工单
  $("auditBox").innerHTML = AUDIT.length
    ? AUDIT.map(a =>
        '<div style="font-size:12px;padding:6px 0;border-bottom:1px dashed var(--hair)">' +
          '<span style="font-weight:650">' + esc(a.action) + '</span>　' +
          '<span style="color:var(--text-3)">' + esc(a.actor_id || "-") + '/' + esc(a.actor_role || "-") +
          '　' + esc(a.from_status) + ' → ' + esc(a.to_status) + '　' + esc(a.created_at) + '</span>' +
          (a.reason ? '<div style="color:var(--text-3);margin-top:3px">' + esc(a.reason) + '</div>' : '') +
        '</div>').join("")
    : '<div class="empty">暂无审计记录（本次改造前的历史工单）</div>';
}

async function doReview(approve) {
  const rec = ((STATS && STATS.recent) || []).find(r => r.id === SEL);
  if (!rec) return;
  const note = ($("noteInput") && $("noteInput").value) || "";
  const label = approve ? "批准" : "驳回";
  if (!confirm(label + "这张工单？" + (approve ? "\n\n会真的执行「待审批动作」并写入审计记录。" : ""))) return;
  try {
    const r = await fetch("/api/tickets/" + encodeURIComponent(rec.ticket_id) + (approve ? "/approve" : "/reject"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: note })
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alert(label + "失败（HTTP " + r.status + "）：" + (d.detail || "")); return; }
    METRIC_SIG = LIST_SIG = DETAIL_SIG = "";   // 强制刷新三个区域
    await load();
  } catch (e) {
    alert("网络异常：" + (e && e.message ? e.message : e));
  }
}

async function load() {
  try {
    const r = await fetch("/stats");
    const data = await r.json();
    STATS = data;
    if (!SEL && data.recent && data.recent.length) SEL = data.recent[0].id;

    const mSig = JSON.stringify([data.total, data.auto, data.escalated, data.by_category]);
    if (mSig !== METRIC_SIG) { METRIC_SIG = mSig; renderMetrics(); }

    const lSig = sigList(data);
    if (lSig !== LIST_SIG) { LIST_SIG = lSig; renderList(); }

    const dSig = sigDetail(data);
    if (dSig !== DETAIL_SIG) { DETAIL_SIG = dSig; renderDetail(); }
  } catch (e) {
    $("list").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>';
  }
}
load();
setInterval(load, 5000);
</script>
</body>
</html>"""


# 演示页面禁用缓存，保证改动即时可见
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """客户端：提交工单（面向使用者，不暴露内部实现细节）。"""
    return HTMLResponse(_USER_PAGE, headers=_NO_CACHE)


@app.get("/ops", response_class=HTMLResponse)
def ops_console(_user: dict = Depends(require_roles("agent", "admin"))) -> HTMLResponse:
    """服务端观测台：工单流水线链路。仅客服/管理员可看（客户不得访问运营数据）。"""
    return HTMLResponse(_OPS_PAGE, headers=_NO_CACHE)
