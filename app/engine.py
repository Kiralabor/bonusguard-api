"""Orchestrazione calcolo: stub oppure Sisal live + cache per utente."""

from __future__ import annotations

import pickle
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .engine_stub import run_extended_calculation as run_stub
from .models import BonusForm, StakeProposal
from .settings import get_settings

# done, total, label — stessa firma di sisal_engine.ProgressCallback
ProgressCallback = Optional[Callable[[int, int, str], None]]

_CACHE_LOCK = threading.Lock()
# user_id -> {ops, at, errors}
_CACHE: Dict[str, dict] = {}
_CACHE_DIR = Path(__file__).resolve().parent.parent / ".quote_cache"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_user_key(user_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", user_id)[:120] or "anon"


def _cache_path(user_id: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{_safe_user_key(user_id)}.pkl"


def _cache_ttl_seconds() -> int:
    return max(0, int(get_settings().cache_ttl_seconds))


def _persist_cache(user_id: str, entry: dict) -> None:
    try:
        _cache_path(user_id).write_bytes(pickle.dumps(entry, protocol=4))
    except Exception:
        pass


def _load_disk_cache(user_id: str) -> Optional[dict]:
    path = _cache_path(user_id)
    if not path.exists():
        return None
    try:
        data = pickle.loads(path.read_bytes())
        ops = list(data.get("ops") or [])
        if not ops:
            return None
        return {
            "ops": ops,
            "at": float(data.get("at") or time.time()),
            "errors": int(data.get("errors") or 0),
        }
    except Exception:
        return None


def _clear_user_cache_locked(user_id: str) -> None:
    _CACHE.pop(user_id, None)
    try:
        path = _cache_path(user_id)
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _entry_fresh(entry: dict) -> bool:
    ttl = _cache_ttl_seconds()
    if ttl > 0 and (time.time() - float(entry.get("at") or 0)) > ttl:
        return False
    return bool(entry.get("ops"))


def _get_fresh_entry_locked(user_id: str) -> Optional[dict]:
    entry = _CACHE.get(user_id)
    if entry is None:
        entry = _load_disk_cache(user_id)
        if entry is not None:
            _CACHE[user_id] = entry
    if entry is None:
        return None
    if not _entry_fresh(entry):
        _clear_user_cache_locked(user_id)
        return None
    return entry


def has_quote_cache(user_id: str) -> bool:
    with _CACHE_LOCK:
        return _get_fresh_entry_locked(user_id) is not None


def quote_cache_remaining_sec(user_id: str) -> int:
    with _CACHE_LOCK:
        entry = _get_fresh_entry_locked(user_id)
        if entry is None:
            return 0
        ttl = _cache_ttl_seconds()
        if ttl <= 0:
            return 0
        return max(0, int(ttl - (time.time() - float(entry["at"]))))


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


def _fetch_opportunities(
    user_id: str,
    *,
    force_refresh: bool = False,
    progress_callback: ProgressCallback = None,
):
    """Scarica mercati Sisal. Cache isolata per user_id."""
    settings = get_settings()

    if not force_refresh:
        with _CACHE_LOCK:
            entry = _get_fresh_entry_locked(user_id)
            if entry is not None:
                return list(entry["ops"]), int(entry["errors"]), True
        raise ValueError(
            "Scansione scaduta o assente (validità 1 ora). "
            "Esegui un nuovo Calcola (1 credito)."
        )

    from . import sisal_engine as eng

    eng.SISAL_API_WORKERS = max(1, settings.max_workers)
    if settings.sisal_worker_url:
        if progress_callback is not None:
            progress_callback(0, 1, "download via gateway IT…")
        ops, errors = _fetch_opportunities_via_it_worker(settings)
        if progress_callback is not None:
            progress_callback(1, 1, "download completato")
    else:
        ops, errors = eng.fetch_sisal_calcio_opportunities(
            catalog_mode=settings.catalog_mode,
            include_extended=settings.include_extended,
            max_workers=settings.max_workers,
            progress_callback=progress_callback,
        )
    entry = {
        "ops": list(ops),
        "at": time.time(),
        "errors": int(errors),
    }
    with _CACHE_LOCK:
        _CACHE[user_id] = entry
        _persist_cache(user_id, entry)
        return list(entry["ops"]), int(entry["errors"]), False


def _fetch_opportunities_via_it_worker(settings) -> tuple[list, int]:
    """Scarica opportunità tramite gateway con IP italiano (PC di casa)."""
    import httpx

    from . import sisal_engine as eng

    if not settings.sisal_worker_secret:
        raise RuntimeError(
            "SISAL_WORKER_URL impostato ma manca SISAL_WORKER_SECRET."
        )

    url = f"{settings.sisal_worker_url}/fetch"
    headers = {"X-Worker-Secret": settings.sisal_worker_secret}
    payload = {
        "catalog_mode": settings.catalog_mode,
        "include_extended": settings.include_extended,
        "max_workers": settings.max_workers,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
            res = client.post(url, json=payload, headers=headers)
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
    user_id: str,
    force_refresh: bool = True,
    progress_callback: ProgressCallback = None,
) -> Tuple[List[StakeProposal], List[str], bool]:
    """Ritorna (results, notes, is_stub). Cache per utente."""
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
        if progress_callback is not None:
            progress_callback(1, 1, "calcolo stub")
        results, stub_notes = run_stub(bonus)
        notes.extend(stub_notes)
        return results, notes, True

    from . import sisal_engine as eng

    ops, errors, from_cache = _fetch_opportunities(
        user_id,
        force_refresh=force_refresh,
        progress_callback=progress_callback,
    )
    notes.append(
        "Ritocco gratis sulla scansione già scaricata."
        if from_cache
        else "Quote scaricate ora (scansione estesa)."
    )
    if errors:
        notes.append(
            f"Attenzione: {errors} richieste non scaricate (risultati possibili incompleti)."
        )

    if progress_callback is not None:
        progress_callback(0, 1, "calcolo puntate…")

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

    if progress_callback is not None:
        progress_callback(1, 1, "calcolo completato")

    return _rows_to_proposals(rows), notes, False
