"""Gestione crediti: memoria locale (dev) oppure Supabase (produzione)."""

from __future__ import annotations

from threading import Lock
from typing import Dict, Optional, Tuple

import httpx

from .settings import get_settings

_STORE: Dict[str, int] = {}
_LOCK = Lock()

DEV_USER_ID = "dev-local-user"
DEV_START_CREDITS = 5

CREDIT_PACKAGES = [
    {"id": "pkg_1", "credits": 1, "price_eur": 2.99, "stripe_price_env": "STRIPE_PRICE_1"},
    {"id": "pkg_5", "credits": 5, "price_eur": 9.99, "stripe_price_env": "STRIPE_PRICE_5"},
    {"id": "pkg_10", "credits": 10, "price_eur": 14.99, "stripe_price_env": "STRIPE_PRICE_10"},
    {"id": "pkg_15", "credits": 15, "price_eur": 19.99, "stripe_price_env": "STRIPE_PRICE_15"},
]


def ensure_dev_user() -> None:
    with _LOCK:
        if DEV_USER_ID not in _STORE:
            _STORE[DEV_USER_ID] = DEV_START_CREDITS


def _local_get(user_id: str) -> int:
    ensure_dev_user()
    with _LOCK:
        return int(_STORE.get(user_id, 0))


def _local_consume(user_id: str, amount: int) -> Tuple[bool, int]:
    ensure_dev_user()
    with _LOCK:
        current = int(_STORE.get(user_id, 0))
        if current < amount:
            return False, current
        current -= amount
        _STORE[user_id] = current
        return True, current


def _local_add(user_id: str, amount: int) -> int:
    ensure_dev_user()
    with _LOCK:
        current = int(_STORE.get(user_id, 0)) + amount
        _STORE[user_id] = current
        return current


def _supabase_headers() -> dict:
    """Header per REST admin. Le nuove chiavi sb_secret_* vanno solo in apikey."""
    settings = get_settings()
    key = settings.supabase_service_role_key
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    # Legacy JWT service_role (eyJ...) richiede anche Authorization Bearer.
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _supabase_get_credits(user_id: str) -> int:
    settings = get_settings()
    url = f"{settings.supabase_url}/rest/v1/profiles"
    with httpx.Client(timeout=20) as client:
        res = client.get(
            url,
            headers=_supabase_headers(),
            params={"id": f"eq.{user_id}", "select": "credits"},
        )
        res.raise_for_status()
        rows = res.json()
        if not rows:
            return 0
        return int(rows[0].get("credits") or 0)


def _supabase_consume(user_id: str, amount: int) -> Tuple[bool, int]:
    """Consume atomico-ish: legge, aggiorna se sufficiente, logga ledger."""
    settings = get_settings()
    current = _supabase_get_credits(user_id)
    if current < amount:
        return False, current
    new_value = current - amount
    headers = _supabase_headers()
    with httpx.Client(timeout=20) as client:
        res = client.patch(
            f"{settings.supabase_url}/rest/v1/profiles",
            headers=headers,
            params={"id": f"eq.{user_id}", "credits": f"gte.{amount}"},
            json={"credits": new_value},
        )
        res.raise_for_status()
        rows = res.json()
        if not rows:
            # Race: qualcun altro ha speso
            return False, _supabase_get_credits(user_id)
        client.post(
            f"{settings.supabase_url}/rest/v1/credit_ledger",
            headers=headers,
            json={
                "user_id": user_id,
                "delta": -amount,
                "reason": "calculation",
            },
        )
        return True, int(rows[0].get("credits") or new_value)


def _supabase_add(user_id: str, amount: int, reason: str) -> int:
    settings = get_settings()
    current = _supabase_get_credits(user_id)
    new_value = current + amount
    headers = _supabase_headers()
    with httpx.Client(timeout=20) as client:
        res = client.patch(
            f"{settings.supabase_url}/rest/v1/profiles",
            headers=headers,
            params={"id": f"eq.{user_id}"},
            json={"credits": new_value},
        )
        res.raise_for_status()
        client.post(
            f"{settings.supabase_url}/rest/v1/credit_ledger",
            headers=headers,
            json={"user_id": user_id, "delta": amount, "reason": reason},
        )
        rows = res.json()
        if rows:
            return int(rows[0].get("credits") or new_value)
    return new_value


def ensure_profile(user_id: str, email: Optional[str] = None) -> None:
    """Crea il profilo se manca (fallback se il trigger auth non ha girato)."""
    if not get_settings().supabase_enabled or user_id == DEV_USER_ID:
        return
    settings = get_settings()
    headers = _supabase_headers()
    with httpx.Client(timeout=20) as client:
        res = client.get(
            f"{settings.supabase_url}/rest/v1/profiles",
            headers=headers,
            params={"id": f"eq.{user_id}", "select": "id"},
        )
        res.raise_for_status()
        if res.json():
            return
        client.post(
            f"{settings.supabase_url}/rest/v1/profiles",
            headers=headers,
            json={"id": user_id, "email": email, "credits": 0},
        )


def get_credits(user_id: str) -> int:
    if get_settings().supabase_enabled and user_id != DEV_USER_ID:
        return _supabase_get_credits(user_id)
    return _local_get(user_id)


def try_consume_credit(user_id: str, amount: int = 1) -> Tuple[bool, int]:
    if get_settings().supabase_enabled and user_id != DEV_USER_ID:
        return _supabase_consume(user_id, amount)
    return _local_consume(user_id, amount)


_LOCAL_REASONS: set[str] = set()


def add_credits(user_id: str, amount: int, reason: str = "manual") -> int:
    if get_settings().supabase_enabled and user_id != DEV_USER_ID:
        return _supabase_add(user_id, amount, reason)
    left = _local_add(user_id, amount)
    _LOCAL_REASONS.add(f"{user_id}|{reason}")
    return left


def refund_credit(user_id: str, amount: int = 1) -> int:
    return add_credits(user_id, amount, reason="calculation_refund")


def package_by_id(package_id: str) -> Optional[dict]:
    for p in CREDIT_PACKAGES:
        if p["id"] == package_id:
            return p
    return None


def ledger_has_reason(user_id: str, reason: str) -> bool:
    """True se quel reason è già in ledger (idempotenza Stripe / welcome)."""
    if get_settings().supabase_enabled and user_id != DEV_USER_ID:
        settings = get_settings()
        headers = _supabase_headers()
        with httpx.Client(timeout=20) as client:
            res = client.get(
                f"{settings.supabase_url}/rest/v1/credit_ledger",
                headers=headers,
                params={
                    "user_id": f"eq.{user_id}",
                    "reason": f"eq.{reason}",
                    "select": "id",
                    "limit": "1",
                },
            )
            if res.status_code != 200:
                return False
            return bool(res.json())
    key = f"{user_id}|{reason}"
    return key in _LOCAL_REASONS


def mark_phone_verified(user_id: str, phone: Optional[str] = None) -> None:
    if not get_settings().supabase_enabled or user_id == DEV_USER_ID:
        return
    settings = get_settings()
    payload: dict = {"phone_verified": True}
    if phone:
        payload["phone"] = phone
    with httpx.Client(timeout=20) as client:
        client.patch(
            f"{settings.supabase_url}/rest/v1/profiles",
            headers=_supabase_headers(),
            params={"id": f"eq.{user_id}"},
            json=payload,
        )


def grant_welcome_credit(user_id: str) -> int:
    """+1 welcome una sola volta (RPC Supabase o store locale)."""
    reason = "welcome_phone_verified"
    if ledger_has_reason(user_id, reason):
        return get_credits(user_id)

    if get_settings().supabase_enabled and user_id != DEV_USER_ID:
        settings = get_settings()
        headers = _supabase_headers()
        mark_phone_verified(user_id)
        with httpx.Client(timeout=20) as client:
            res = client.post(
                f"{settings.supabase_url}/rest/v1/rpc/grant_welcome_credit",
                headers=headers,
                json={"p_user": user_id},
            )
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, int):
                    return data
                if isinstance(data, list) and data:
                    return int(data[0] or 0)
                return get_credits(user_id)
        return add_credits(user_id, 1, reason=reason)

    return add_credits(user_id, 1, reason=reason)
