from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from . import credits
from . import jobs
from .auth import resolve_user
from .engine import has_quote_cache, run_bonus_calculation, utc_now
from .sisal_engine import normalize_catalog_mode
from .models import (
    CalculationJobStart,
    CalculationJobStatus,
    CalculationRequest,
    CalculationResponse,
    CreditPackage,
    MeResponse,
    PackagesResponse,
)
from .settings import get_settings
from . import stripe_billing

app = FastAPI(
    title="Sisal BonusGuard API",
    description=(
        "API server-side per calcoli estesi su bonus Sisal. "
        "Le fonti quote restano sul server; l'app vede solo i risultati."
    ),
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _auth_user(authorization: str | None, access_token: str | None = None):
    header = authorization
    if access_token:
        header = f"Bearer {access_token}"
    return resolve_user(header)


@app.get("/health")
def health():
    s = get_settings()
    return {
        "ok": True,
        "service": "bonusguard-api",
        "engine_mode": "stub" if s.use_stub_engine else "live",
        "supabase": s.supabase_enabled,
        "stripe": s.stripe_enabled,
        "sisal_proxy": bool(s.sisal_http_proxy),
        "sisal_worker": bool(s.sisal_worker_url),
        "catalog_mode": s.catalog_mode,
        "catalog_resolved": normalize_catalog_mode(s.catalog_mode),
        "include_extended": s.include_extended,
    }


@app.get("/me", response_model=MeResponse)
def me(authorization: str | None = Header(default=None)):
    user = _auth_user(authorization)
    try:
        credits.ensure_profile(user.user_id, user.email)
    except Exception:
        pass
    return MeResponse(
        user_id=user.user_id,
        email=user.email,
        credits=credits.get_credits(user.user_id),
        phone_verified=user.phone_verified,
    )


@app.get("/packages", response_model=PackagesResponse)
def packages():
    return PackagesResponse(
        packages=[
            CreditPackage(id=p["id"], credits=p["credits"], price_eur=p["price_eur"])
            for p in credits.CREDIT_PACKAGES
        ]
    )


@app.post("/calculations", response_model=CalculationJobStart)
def calculations(
    body: CalculationRequest,
    authorization: str | None = Header(default=None),
):
    """1 credito = 1 scansione estesa nuova (job asincrono: evita timeout Render ~100s)."""
    user = _auth_user(authorization, body.access_token)
    _validate_bonus_form(body.bonus)

    ok, left = credits.try_consume_credit(user.user_id, 1)
    if not ok:
        raise HTTPException(
            status_code=402,
            detail="Crediti insufficienti. Acquista un pacchetto per calcolare.",
        )

    job_id = jobs.start_calculation_job(user.user_id, body.bonus)
    return CalculationJobStart(job_id=job_id, status="running", credits_left=left)


@app.get("/calculations/jobs/{job_id}", response_model=CalculationJobStatus)
def calculation_job_status(
    job_id: str,
    authorization: str | None = Header(default=None),
):
    user = _auth_user(authorization)
    job = jobs.get_job(job_id)
    if job is None or job.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Job non trovato o scaduto.")
    # Mentre gira, non allegare payload pesanti (solo status).
    return CalculationJobStatus(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        error=job.error,
        result=job.response if job.status == "done" else None,
    )


def _validate_bonus_form(bonus) -> None:
    step = float(bonus.step)
    allowed_steps = {0.05, 0.10, 0.50, 1.00}
    if round(step, 2) not in allowed_steps:
        raise HTTPException(status_code=400, detail="Step non valido.")
    budget_cents = int(round(bonus.playable_budget * 100))
    step_cents = int(round(step * 100))
    if step_cents <= 0 or budget_cents % step_cents != 0:
        raise HTTPException(
            status_code=400,
            detail=f"Il budget deve essere multiplo di {step:.2f} €.",
        )


@app.post("/calculations/rerank", response_model=CalculationResponse)
def calculations_rerank(
    body: CalculationRequest,
    authorization: str | None = Header(default=None),
):
    """Ritocco gratis: ricalcola puntate sulla scansione già in cache (0 crediti)."""
    user = _auth_user(authorization, body.access_token)
    _validate_bonus_form(body.bonus)

    if not has_quote_cache():
        raise HTTPException(
            status_code=409,
            detail="Scansione scaduta o assente. Esegui un nuovo Calcola (1 credito).",
        )

    try:
        results, notes, is_stub = run_bonus_calculation(body.bonus, force_refresh=False)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return CalculationResponse(
        credits_left=credits.get_credits(user.user_id),
        fetched_at=utc_now(),
        mode="ritocco",
        stub=is_stub,
        results=results,
        notes=notes,
    )


@app.post("/billing/checkout")
def billing_checkout(
    package_id: str,
    authorization: str | None = Header(default=None),
):
    user = _auth_user(authorization)
    try:
        session = stripe_billing.create_checkout_session(user.user_id, package_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "checkout_url": session.get("url"),
        "session_id": session.get("id"),
    }


@app.post("/billing/webhook")
async def billing_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="stripe-signature"),
):
    payload = await request.body()
    try:
        result = stripe_billing.handle_webhook(payload, stripe_signature)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Webhook error: {exc}") from exc
    return result


@app.post("/dev/add-credits")
def dev_add_credits(amount: int = 5, authorization: str | None = Header(default=None)):
    """Solo sviluppo: ricarica crediti senza Stripe."""
    if not get_settings().allow_dev_credit_topup:
        raise HTTPException(status_code=403, detail="Dev top-up disabilitato.")
    if amount < 1 or amount > 100:
        raise HTTPException(status_code=400, detail="amount tra 1 e 100")
    user = _auth_user(authorization)
    left = credits.add_credits(user.user_id, amount, reason="dev_topup")
    return {"credits": left, "added": amount}


@app.post("/dev/welcome-after-phone")
def dev_welcome_after_phone(authorization: str | None = Header(default=None)):
    """Stub welcome +1 dopo verifica telefono (locale). Con Supabase userai RPC grant_welcome_credit."""
    user = _auth_user(authorization)
    # In locale non tracciamo welcome_granted: aggiunge sempre 1 in dev.
    left = credits.add_credits(user.user_id, 1, reason="welcome_phone_verified")
    return {"credits": left, "added": 1, "phone_verified": True}
