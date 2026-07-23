from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import credits
from . import jobs
from .auth import AuthError, resolve_user
from .engine import (
    has_quote_cache,
    quote_cache_remaining_sec,
    run_bonus_calculation,
    utc_now,
)
from .rate_limit import check_rate_limit, client_ip
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
    version="0.3.0",
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _auth_user(authorization: str | None, access_token: str | None = None):
    header = authorization
    if access_token:
        header = f"Bearer {access_token}"
    try:
        return resolve_user(header)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.detail) from exc


@app.get("/health")
def health():
    s = get_settings()
    return {
        "ok": True,
        "service": "bonusguard-api",
        "engine_mode": "stub" if s.use_stub_engine else "live",
        "supabase": s.supabase_enabled,
        "stripe": s.stripe_enabled,
        "stripe_ready": s.stripe_ready,
        "dev_topup": s.allow_dev_credit_topup,
        "sisal_proxy": bool(s.sisal_http_proxy),
        "sisal_worker": bool(s.sisal_worker_url),
        "catalog_mode": s.catalog_mode,
        "catalog_resolved": normalize_catalog_mode(s.catalog_mode),
        "include_extended": s.include_extended,
        "quote_cache_ttl_sec": s.cache_ttl_seconds,
        # Cache è per-utente: senza auth non esporre stato globale.
        "quote_cache": False,
        "quote_cache_remaining_sec": 0,
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
        quote_cache=has_quote_cache(user.user_id),
        quote_cache_remaining_sec=quote_cache_remaining_sec(user.user_id),
    )


@app.post("/me/claim-welcome")
def claim_welcome(authorization: str | None = Header(default=None)):
    """+1 credito dopo verifica telefono Supabase (phone_confirmed)."""
    user = _auth_user(authorization)
    if not user.phone_confirmed and not get_settings().allow_dev_credit_topup:
        raise HTTPException(
            status_code=400,
            detail="Telefono non verificato. Completa l'OTP SMS prima.",
        )
    if user.phone_verified and credits.ledger_has_reason(
        user.user_id, "welcome_phone_verified"
    ):
        return {
            "credits": credits.get_credits(user.user_id),
            "added": 0,
            "phone_verified": True,
        }
    left = credits.grant_welcome_credit(user.user_id)
    return {"credits": left, "added": 1, "phone_verified": True}


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
    request: Request,
    body: CalculationRequest,
    authorization: str | None = Header(default=None),
):
    """1 credito = 1 scansione estesa nuova (job asincrono)."""
    s = get_settings()
    check_rate_limit(
        f"calc:{client_ip(request)}",
        max_hits=s.rate_limit_calc_per_min,
        window_sec=60,
    )
    user = _auth_user(authorization, body.access_token)
    check_rate_limit(
        f"calc-user:{user.user_id}",
        max_hits=s.rate_limit_calc_per_min,
        window_sec=60,
    )
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
    return CalculationJobStatus(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        progress_pct=float(getattr(job, "progress_pct", 0.0) or 0.0),
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
    request: Request,
    body: CalculationRequest,
    authorization: str | None = Header(default=None),
):
    """Ritocco gratis: ricalcola puntate sulla scansione già in cache (0 crediti)."""
    s = get_settings()
    check_rate_limit(
        f"rerank:{client_ip(request)}",
        max_hits=s.rate_limit_auth_per_min,
        window_sec=60,
    )
    user = _auth_user(authorization, body.access_token)
    _validate_bonus_form(body.bonus)

    if not has_quote_cache(user.user_id):
        raise HTTPException(
            status_code=409,
            detail="Scansione scaduta o assente (validità 1 ora). Esegui un nuovo Calcola (1 credito).",
        )

    try:
        results, notes, is_stub = run_bonus_calculation(
            body.bonus,
            user_id=user.user_id,
            force_refresh=False,
        )
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


@app.get("/billing/success", response_class=HTMLResponse)
def billing_success():
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:2rem'>"
        "<h1>Pagamento ricevuto</h1>"
        "<p>Torna nell'app Sisal BonusGuard e aggiorna i crediti.</p>"
        "</body></html>"
    )


@app.get("/billing/cancel", response_class=HTMLResponse)
def billing_cancel():
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:2rem'>"
        "<h1>Pagamento annullato</h1>"
        "<p>Nessun addebito. Puoi riprovare dallo shop in app.</p>"
        "</body></html>"
    )


@app.get("/legal/privacy", response_class=HTMLResponse)
def legal_privacy():
    return HTMLResponse(_LEGAL_PRIVACY)


@app.get("/legal/terms", response_class=HTMLResponse)
def legal_terms():
    return HTMLResponse(_LEGAL_TERMS)


@app.get("/dev/sisal-probe")
def sisal_probe():
    """Diagnostica connessione Sisal dal server (solo DEV)."""
    if not get_settings().allow_dev_credit_topup:
        raise HTTPException(status_code=403, detail="Probe disabilitato.")
    from . import sisal_engine as eng

    proxy = eng._sisal_proxy_url()
    try:
        session = eng._new_sisal_session()
        alberatura = eng._load_alberatura(session)
        keys = (
            (alberatura.get("manifestazioneListByDisciplinaTutti") or {}).get(
                eng.SISAL_CALCIO_DISCIPLINA, []
            )
            or []
        )
        return {
            "ok": True,
            "proxy": proxy or None,
            "competizioni": len(keys),
            "bytes_hint": len(str(alberatura)),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "proxy": proxy or None,
            "error": str(exc),
            "friendly": eng.format_sisal_error(exc),
        }


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
    """Solo DEV: welcome senza OTP reale."""
    if not get_settings().allow_dev_credit_topup:
        raise HTTPException(status_code=403, detail="Dev welcome disabilitato.")
    user = _auth_user(authorization)
    left = credits.grant_welcome_credit(user.user_id)
    return {"credits": left, "added": 1, "phone_verified": True}


_LEGAL_PRIVACY = """<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<title>Privacy Policy — Sisal BonusGuard</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#111}
h1{font-size:1.5rem}h2{font-size:1.1rem;margin-top:1.5rem}</style></head><body>
<h1>Privacy Policy</h1>
<p>Ultimo aggiornamento: luglio 2026.</p>
<p>Sisal BonusGuard («l'App») è un servizio indipendente, non ufficiale e non affiliato a Sisal.</p>
<h2>Dati trattati</h2>
<ul>
<li>Account: email e, se abilitato, numero di telefono (Supabase Auth).</li>
<li>Utilizzo: crediti, cronologia acquisti (Stripe), parametri di calcolo inviati al server.</li>
<li>Tecnici: log server, indirizzo IP, diagnostica errori.</li>
</ul>
<h2>Finalità</h2>
<p>Fornire il servizio di calcolo, autenticazione, fatturazione crediti, prevenzione abusi e obblighi di legge.</p>
<h2>Base giuridica</h2>
<p>Esecuzione del contratto e legittimo interesse (sicurezza/abuso). Consenso dove richiesto (es. marketing, se attivato).</p>
<h2>Conservazione</h2>
<p>I dati dell'account restano finché l'account è attivo. Puoi richiedere cancellazione contattando il supporto.</p>
<h2>Diritti</h2>
<p>Accesso, rettifica, cancellazione, limitazione, opposizione, portabilità (GDPR). Contatto: support@kiralab.com</p>
<h2>Terze parti</h2>
<p>Supabase (auth/DB), Stripe (pagamenti), hosting Render. Le quote sportive sono scaricate da fonti pubbliche lato server.</p>
</body></html>"""

_LEGAL_TERMS = """<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<title>Termini di servizio — Sisal BonusGuard</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#111}
h1{font-size:1.5rem}h2{font-size:1.1rem;margin-top:1.5rem}</style></head><body>
<h1>Termini di servizio</h1>
<p>Ultimo aggiornamento: luglio 2026.</p>
<h2>Natura del servizio</h2>
<p>L'App fornisce strumenti di calcolo informativo su scenari di puntata. Non è un bookmaker, non accetta scommesse e non è affiliata a Sisal.</p>
<h2>Età</h2>
<p>Il servizio è riservato a maggiorenni (18+). Dichiarando di usare l'App confermi di avere almeno 18 anni.</p>
<h2>Nessuna garanzia</h2>
<p>Le quote cambiano in tempo reale. Non garantiamo prelievo del bonus, guadagno, o allineamento con l'offerta Sisal. Uso a proprio rischio.</p>
<h2>Crediti</h2>
<p>1 credito = 1 scansione completa. I ritocchi filtri sulla stessa scansione (entro 1 ora) sono gratis. I crediti acquistati non sono rimborsabili salvo obblighi di legge o errore tecnico imputabile a noi.</p>
<h2>Account</h2>
<p>Sei responsabile delle credenziali. Abusi, automazioni aggressive o tentativi di aggirare i pagamenti possono comportare sospensione.</p>
<h2>Contatti</h2>
<p>support@kiralab.com</p>
</body></html>"""
