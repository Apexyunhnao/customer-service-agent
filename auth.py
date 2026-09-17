"""下游服务的认证校验 —— 只验签，不签发。

**为什么下游还要验一遍**：编排层传来的角色字符串**不可信**（它可能被绕过、被改错，
或者请求根本不是从编排层来的）。下游服务拿到同一枚 JWT、用同一个 AUTH_SECRET 自己验签，
才知道调用方到底是谁 —— 这是纵深防御，也是任务书第五章对客服 Agent 的明确要求。

Token 来源优先级：
1. `Authorization: Bearer <token>` —— 服务间调用（编排器转发）
2. `Cookie: svc_token=<token>` —— 浏览器直连

安全默认：AUTH_SECRET 未配置时**拒绝启动**（fail-closed），不回落到固定默认密钥。
"""

import os
from typing import Any

import jwt
from dotenv import load_dotenv
from fastapi import Cookie, Depends, Header, HTTPException, status

# 本模块自己加载 .env：保证无论被谁、以什么顺序导入，AUTH_SECRET 都能读到正确值
# （上游 orchestrator 曾因导入顺序问题回落到默认密钥，导致跨服务验签全部失败）
load_dotenv()

def _require_auth_secret() -> str:
    """读取 AUTH_SECRET；未配置直接拒绝启动（fail-closed）。

    历史：这里原本回落到固定默认值 `dev-only-insecure-secret-change-me` ——
    任何人 clone 后不配密钥也能跑起来，而"能跑"的样子和配好密钥一模一样。
    现在改成缺失即拒绝：启动失败比"看起来正常的假安全"好。
    """
    secret = (os.getenv("AUTH_SECRET") or "").strip()
    if not secret:
        raise RuntimeError(
            "\n[AUTH_SECRET 未配置] 服务拒绝启动（fail-closed）。\n"
            "  配一个与 orchestrator / rag 完全相同的 AUTH_SECRET（三仓共享验签）。\n"
            "  最简单：在项目根目录运行 start-demo.bat（会生成一次随机密钥并写入三仓 .env）\n"
            "  或：cp .env.example .env 后填入 "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\" 的输出\n"
        )
    return secret


SECRET_KEY: str = _require_auth_secret()
ALGORITHM = "HS256"
COOKIE_NAME = "svc_token"


def decode_token(token: str) -> dict[str, Any]:
    """验签 + 校验过期；失败一律 401（不泄露是签名错还是已过期）。"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="凭证无效，请重新登录")


def current_user(
    svc_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """FastAPI 依赖：解析并验签调用方身份。Bearer 优先（服务间），其次 Cookie（浏览器）。"""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        token = svc_token or ""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")

    payload = decode_token(token)
    return {
        "username": payload.get("sub", ""),
        "role": payload.get("role", ""),
        "owner_id": payload.get("owner_id", ""),
    }


def require_roles(*roles: str):
    """依赖工厂：要求调用方角色在 roles 内，否则 403。用于写操作的权限收口。"""
    allowed = set(roles)

    def _guard(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user.get("role") not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前角色无权执行该操作",
            )
        return user

    return _guard
