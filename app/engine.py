"""Orchestrazione calcolo: stub oppure Sisal live + cache in RAM/disco."""

from __future__ import annotations

import pickle
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .engine_stub import run_extended_calculation as run_stub
from .models import BonusForm, StakeProposal
from .settings import get_settings

_CACHE_LOCK = threading.Lock()
_CACHE_OPS: list = []
_CACHE_AT: float = 0.0
_CACHE_ERRORS: int = 0
# Persistenza: i ritocchi gratis devono funzionare anche dopo reload uvicorn.
_CACHE_FILE = Path(__file__).resolve().parent.parent / ".quote_cache.pkl"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _persist_cache(ops: list, errors: int) -> None:
    try:
        _CACHE_FILE.write_bytes(
            pickle.dumps({"ops": ops, "errors": int(errors), "at": time.time()}, protocol=4)
        )
    except Exception:
        pass


def _load_disk_cache() -> bool:
    """Carica cache da disco in RAM se presente. Ritorna True se ok."""
    global _CACHE_OPS, _CACHE_AT, _CACHE_ERRORS
    if not _CACHE_FILE.exists():
        return False
    try:
        data = pickle.loads(_CACHE_FILE.read_bytes())
        ops = list(data.get("ops") or [])
        if not ops:
            return False
        _CACHE_OPS = ops
        _CACHE_AT = float(data.get("at") or time.time())
        _CACHE_ERRORS = int(data.get("errors") or 0)
        return True
    except Exception:
        return False


def _rows_to_proposals(rows: list) -> List[StakeProposal]:
    out: List[StakeProposal] = []
    for row in rows:
        out.append(
            StakeProposal(
                data=str(row.get("Data") or ""),
                ora=str(row.get("Ora") or ""),
                partita=str(row.get("Partita") or ""),
                mercato=str(row.get("Tipo scommessa") or ""),
                esiti=list(row.get("_esiti") or []),
                quote=[float(q) for q in (row.get("_quote") or [])],
                puntate=[float(p) for p in (row.get("_puntate") or [])],
                rientro_minimo=float(row.get("Rientro minimo") or 0),
                perdita_euro=float(row.get("Perdita €") or 0),
                perdita_pct=float(row.get("Perdita %") or 0),
                open_url=str(row.get("_url") or ""),
            )
        )
    return out


def has_quote_cache() -> bool:
    with _CACHE_LOCK:
        if _CACHE_OPS:
            return True
        return _load_disk_cache()


def _fetch_opportunities(*, force_refresh: bool = False):
    """Scarica mercati Sisal.

    force_refresh=True: nuova scansione (calcolo a pagamento).
    force_refresh=False: solo cache della scansione già pagata (ritocchi gratis).
    """
    settings = get_settings()
    global _CACHE_OPS, _CACHE_AT, _CACHE_ERRORS

    if not force_refresh:
        with _CACHE_LOCK:
            if _CACHE_OPS:
                return list(_CACHE_OPS), _CACHE_ERRORS, True
            if _load_disk_cache():
                return list(_CACHE_OPS), _CACHE_ERRORS, True
        raise ValueError(
            "Nessuna scansione in memoria. Torna indietro ed esegui un nuovo Calcola (1 credito)."
        )

    from . import sisal_engine as eng

    # Worker limit server-side (non esposto all'app).
    eng.SISAL_API_WORKERS = max(1, settings.max_workers)
    if settings.sisal_worker_url:
        ops, errors = _fetch_opportunities_via_it_worker(settings)
    else:
        ops, errors = eng.fetch_sisal_calcio_opportunities(
            catalog_mode=settings.catalog_mode,
            include_extended=settings.include_extended,
            max_workers=settings.max_workers,
        )
    with _CACHE_LOCK:
        _CACHE_OPS = list(ops)
        _CACHE_AT = time.time()
        _CACHE_ERRORS = int(errors)
        _persist_cache(_CACHE_OPS, _CACHE_ERRORS)
        return list(_CACHE_OPS), _CACHE_ERRORS, False


def _fetch_opportunities_via_it_worker(settings) -> tuple[list, int]:
    """Scarica opportunità tramite gateway con IP italiano (PC di casa)."""
    import httpx

    from . import sisal_engine as eng

    if not settings.sisal_worker_secret:
        raise RuntimeError(
            "SISAL_WORKER_URL impostato ma manca SISAL_WORKER_SECRET."
        )

    url = f"{settings.sisal_worker_url}/scan"
    try:
        with httpx.Client(timeout=httpx.Timeout(480.0, connect=30.0)) as client:
            res = client.post(
                url,
                headers={"Authorization": f"Bearer {settings.sisal_worker_secret}"},
                json={
                    "catalog_mode": settings.catalog_mode,
                    "include_extended": settings.include_extended,
                    "max_workers": settings.max_workers,
                },
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            "Timeout verso il gateway italiano. "
            "Verifica che il PC sia acceso e il tunnel attivo."
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Gateway italiano non raggiungibile: {exc}. "
            "Avvia run_sisal_worker_it.bat sul PC."
        ) from exc

    if res.status_code >= 400:
        detail = res.text
        try:
            detail = res.json().get("detail", detail)
        except Exception:
            pass
        raise RuntimeError(f"Gateway IT errore {res.status_code}: {detail}")

    payload = res.json()
    ops = []
    for item in payload.get("opportunities") or []:
        ops.append(
            eng.MarketOpportunity(
                data=str(item.get("data") or ""),
                ora=str(item.get("ora") or ""),
                partita=str(item.get("partita") or ""),
                mercato=str(item.get("mercato") or ""),
                esiti=list(item.get("esiti") or []),
                quote=[float(q) for q in (item.get("quote") or [])],
                url=str(item.get("url") or ""),
                pal=int(item.get("pal") or 0),
                avv=int(item.get("avv") or 0),
            )
        )
    return ops, int(payload.get("errors") or 0)


def run_bonus_calculation(
    bonus: BonusForm,
    *,
    force_refresh: bool = True,
) -> Tuple[List[StakeProposal], List[str], bool]:
    """Ritorna (results, notes, is_stub).

    force_refresh=True (default): nuova scansione Sisal — usato da /calculations a pagamento.
    force_refresh=False: riusa cache — solo per ritocchi gratis sulla stessa analisi.
    """
    settings = get_settings()
    notes: List[str] = []

    notes.append(
        f"Bonus {bonus.bonus_amount:.2f} EUR · budget puntate "
        f"{bonus.playable_budget:.2f} € (step {bonus.step:.2f} €)."
    )
    if bonus.rollover_multiplier and abs(bonus.rollover_multiplier - 1.0) > 1e-9:
        required_play = round(bonus.bonus_amount * bonus.rollover_multiplier, 2)
        notes.append(
            f"Rollover x{bonus.rollover_multiplier:g} → volume stimato ~ {required_play:.2f} EUR."
        )

    if settings.use_stub_engine:
        results, stub_notes = run_stub(bonus)
        notes.extend(stub_notes)
        return results, notes, True

    from . import sisal_engine as eng

    ops, errors, from_cache = _fetch_opportunities(force_refresh=force_refresh)
    notes.append(
        "Ritocco gratis sulla scansione già scaricata."
        if from_cache
        else "Quote scaricate ora (scansione estesa)."
    )
    if errors:
        notes.append(
            f"Attenzione: {errors} richieste non scaricate (risultati possibili incompleti)."
        )

    filtered = [
        item for item in ops if eng.opportunity_meets_minimum_odd(item, bonus.min_odd)
    ]
    excluded = len(ops) - len(filtered)
    if bonus.min_odd is not None:
        notes.append(f"Scartate {excluded} scommesse sotto quota {bonus.min_odd:.2f}.")

    if not filtered:
        raise ValueError(
            "Nessuna scommessa resta con questi filtri. "
            "Prova ad abbassare la quota minima."
        )

    rows, skipped_min = eng.rank_opportunities(
        filtered,
        budget=float(bonus.playable_budget),
        step=float(bonus.step),
        top_n=int(bonus.top_n),
        min_stake=eng.MIN_STAKE,
    )
    if skipped_min:
        notes.append(
            f"Escluse {skipped_min} proposte con puntata sotto {eng.MIN_STAKE:.2f} €."
        )
    if not rows:
        raise ValueError(
            "Nessuna proposta con puntate valide per questo budget/step."
        )

    return _rows_to_proposals(rows), notes, False
