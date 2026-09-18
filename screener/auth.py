"""Supabase Auth JWT 検証（FastAPI 依存性）。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)


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
        "supabase_url": url or None,
        "supabase_anon_key": anon_key or None,
    }


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
) -> AuthUser:
    """
    認証必須モード: Bearer トークン必須。
    未設定モード: ゲスト（id=None）— ローカル開発向け。
    """
    if not is_auth_enabled():
        return AuthUser(id=None)

    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="ログインが必要です。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verify_access_token(token)


def require_user_id(user: AuthUser) -> str:
    """認証必須モードで user_id を返す。未設定モードは None。"""
    if not is_auth_enabled():
        return None  # type: ignore[return-value]
    if not user.id:
        raise HTTPException(status_code=401, detail="ログインが必要です。")
    return user.id
