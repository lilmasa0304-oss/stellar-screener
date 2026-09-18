"""Supabase Auth JWT / 匿名クライアント ID 検証（FastAPI 依存性）。"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

_CLIENT_USER_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AuthUser:
    """認証済みユーザー（未設定時は id=None のゲスト）。"""

    id: Optional[str]
    email: Optional[str] = None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.id)


def is_auth_enabled() -> bool:
    """SUPABASE_JWT_SECRET が設定されていれば認証必須モード。"""
    return bool(os.getenv("SUPABASE_JWT_SECRET", "").strip())


def get_supabase_public_config() -> dict:
    """フロントエンド用の Supabase 公開設定（anon key のみ）。"""
    url = os.getenv("SUPABASE_URL", "").strip()
    anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    return {
        "enabled": is_auth_enabled(),
        "anonymous_auth": True,
        "client_user_header": "X-Client-User-Id",
        "supabase_url": url or None,
        "supabase_anon_key": anon_key or None,
    }


def _normalize_client_user_id(raw: Optional[str]) -> Optional[str]:
    """ブラウザ localStorage 由来の匿名 UUID を検証する。"""
    value = str(raw or "").strip()
    if not value or not _CLIENT_USER_ID_RE.match(value):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def verify_access_token(token: str) -> AuthUser:
    """Supabase アクセストークンを検証し AuthUser を返す。"""
    secret = os.getenv("SUPABASE_JWT_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="認証が設定されていません。")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="authenticated",
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="セッションの有効期限が切れました。") from exc
    except jwt.InvalidTokenError as exc:
        logger.debug("JWT 検証失敗: %s", exc)
        raise HTTPException(status_code=401, detail="無効な認証トークンです。") from exc

    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="ユーザー ID を取得できません。")

    email = payload.get("email")
    return AuthUser(id=user_id, email=str(email) if email else None)


def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_client_user_id: Optional[str] = Header(None, alias="X-Client-User-Id"),
) -> AuthUser:
    """
    ユーザー識別（優先順）:
    1. Supabase JWT（匿名ログイン含む）
    2. ブラウザ永続 UUID（X-Client-User-Id）
    3. 未設定モード: id=None（ローカル開発・スコープなし）
    """
    token = _extract_bearer_token(authorization)
    if token and is_auth_enabled():
        return verify_access_token(token)

    client_id = _normalize_client_user_id(x_client_user_id)
    if client_id:
        return AuthUser(id=client_id)

    if is_auth_enabled():
        raise HTTPException(
            status_code=401,
            detail="匿名セッションを確立できません。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthUser(id=None)


def require_user_id(user: AuthUser) -> Optional[str]:
    """user_id を返す。JWT/匿名 ID モードでは id 必須。"""
    if user.id:
        return user.id
    if is_auth_enabled():
        raise HTTPException(status_code=401, detail="匿名セッションを確立できません。")
    return None
