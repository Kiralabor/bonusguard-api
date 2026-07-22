"""Risoluzione utente da Authorization Bearer (Supabase JWT o stub)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from . import credits
from .settings import get_settings


@dataclass
class AuthUser:
    user_id: str
    email: Optional[str] = None
    phone_verified: bool = False


def resolve_user(authorization: str | None) -> AuthUser:
    settings = get_settings()
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if not token:
        return AuthUser(
            user_id=credits.DEV_USER_ID,
            email="dev@local.test",
            phone_verified=True,
        )

    if settings.supabase_enabled:
        user = _supabase_user(token)
        if user:
            return user

    # Fallback stub: token opaco → user id derivato (solo dev)
    return AuthUser(user_id=f"token:{token[:32]}", email=None, phone_verified=False)


def _supabase_user(access_token: str) -> Optional[AuthUser]:
    settings = get_settings()
    url = f"{settings.supabase_url}/auth/v1/user"
    # apikey = secret/publishable; Authorization = JWT utente (non sb_secret)
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {access_token}",
    }
    try:
        with httpx.Client(timeout=15) as client:
            res = client.get(url, headers=headers)
            if res.status_code != 200:
                return None
            data = res.json()
            user_id = data.get("id")
            if not user_id:
                return None
            phone_confirmed = bool(data.get("phone_confirmed_at") or data.get("phone"))
            # profili.phone_verified è la fonte business
            profile = _profile(user_id)
            return AuthUser(
                user_id=user_id,
                email=data.get("email"),
                phone_verified=bool(profile.get("phone_verified")) if profile else phone_confirmed,
            )
    except Exception:
        return None


def _profile(user_id: str) -> dict:
    settings = get_settings()
    url = f"{settings.supabase_url}/rest/v1/profiles"
    key = settings.supabase_service_role_key
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
    }
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    with httpx.Client(timeout=15) as client:
        res = client.get(
            url,
            headers=headers,
            params={"id": f"eq.{user_id}", "select": "phone_verified,credits,email"},
        )
        if res.status_code != 200:
            return {}
        rows = res.json()
        return rows[0] if rows else {}
