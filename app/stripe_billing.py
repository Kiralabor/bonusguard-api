"""Stripe Checkout (test). Accredito crediti solo via webhook firmato."""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from . import credits
from .settings import get_settings


def create_checkout_session(user_id: str, package_id: str) -> dict:
    settings = get_settings()
    if not settings.stripe_enabled:
        raise RuntimeError("Stripe non configurato (STRIPE_SECRET_KEY mancante).")

    pack = credits.package_by_id(package_id)
    if not pack:
        raise ValueError("Pacchetto non valido.")

    price_id = os.getenv(pack["stripe_price_env"], "").strip()
    if not price_id:
        # Fallback: prezzo ad-hoc in centesimi (solo test, senza Price creato in Dashboard)
        return _checkout_with_price_data(user_id, pack)

    data = {
        "mode": "payment",
        "success_url": settings.stripe_success_url,
        "cancel_url": settings.stripe_cancel_url,
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "metadata[user_id]": user_id,
        "metadata[package_id]": package_id,
        "metadata[credits]": str(pack["credits"]),
        "client_reference_id": user_id,
    }
    return _stripe_form_post("/v1/checkout/sessions", data)


def _checkout_with_price_data(user_id: str, pack: dict) -> dict:
    settings = get_settings()
    cents = int(round(float(pack["price_eur"]) * 100))
    data = {
        "mode": "payment",
        "success_url": settings.stripe_success_url,
        "cancel_url": settings.stripe_cancel_url,
        "line_items[0][price_data][currency]": "eur",
        "line_items[0][price_data][product_data][name]": f"BonusGuard {pack['credits']} crediti",
        "line_items[0][price_data][unit_amount]": str(cents),
        "line_items[0][quantity]": "1",
        "metadata[user_id]": user_id,
        "metadata[package_id]": pack["id"],
        "metadata[credits]": str(pack["credits"]),
        "client_reference_id": user_id,
    }
    return _stripe_form_post("/v1/checkout/sessions", data)


def _stripe_form_post(path: str, data: dict) -> dict:
    settings = get_settings()
    with httpx.Client(timeout=30) as client:
        res = client.post(
            f"https://api.stripe.com{path}",
            data=data,
            auth=(settings.stripe_secret_key, ""),
        )
        if res.status_code >= 400:
            raise RuntimeError(f"Stripe error: {res.text}")
        return res.json()


def handle_webhook(payload: bytes, sig_header: Optional[str]) -> dict[str, Any]:
    """Verifica firma se presente secret; accredita crediti su checkout.session.completed."""
    settings = get_settings()
    import json

    if settings.stripe_webhook_secret:
        _verify_stripe_signature(payload, sig_header or "", settings.stripe_webhook_secret)

    event = json.loads(payload.decode("utf-8"))
    etype = event.get("type")
    if etype != "checkout.session.completed":
        return {"ok": True, "ignored": etype}

    session = event.get("data", {}).get("object", {})
    meta = session.get("metadata") or {}
    user_id = meta.get("user_id") or session.get("client_reference_id")
    credits_raw = meta.get("credits")
    if not user_id or not credits_raw:
        return {"ok": False, "error": "metadata mancante"}

    amount = int(credits_raw)
    left = credits.add_credits(user_id, amount, reason=f"stripe:{session.get('id')}")
    return {"ok": True, "user_id": user_id, "added": amount, "credits": left}


def _verify_stripe_signature(payload: bytes, header: str, secret: str) -> None:
    """Verifica semplificata Stripe-Signature (t=...,v1=...)."""
    import hashlib
    import hmac
    import time

    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        raise PermissionError("Firma Stripe assente.")
    if abs(time.time() - int(timestamp)) > 300:
        raise PermissionError("Firma Stripe scaduta.")
    signed = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PermissionError("Firma Stripe non valida.")
