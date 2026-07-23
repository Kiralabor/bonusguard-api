"""Job asincroni per scansioni lunghe (Render Free taglia HTTP ~100s)."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import credits
from .engine import run_bonus_calculation, utc_now
from .models import BonusForm, CalculationResponse


@dataclass
class CalcJob:
    id: str
    user_id: str
    status: str = "running"  # running | done | error
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: str = "avvio"
    progress_pct: float = 0.0
    error: Optional[str] = None
    response: Optional[CalculationResponse] = None


_LOCK = threading.Lock()
_JOBS: dict[str, CalcJob] = {}


def start_calculation_job(user_id: str, bonus: BonusForm) -> str:
    job_id = uuid.uuid4().hex
    job = CalcJob(id=job_id, user_id=user_id)
    with _LOCK:
        _JOBS[job_id] = job
        _prune_locked()

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, user_id, bonus),
        daemon=True,
        name=f"calc-{job_id[:8]}",
    )
    thread.start()
    return job_id


def get_job(job_id: str) -> Optional[CalcJob]:
    with _LOCK:
        return _JOBS.get(job_id)


def job_to_public(job: CalcJob) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "progress_pct": job.progress_pct,
    }
    if job.error:
        out["error"] = job.error
    if job.response is not None:
        out["result"] = job.response.model_dump(mode="json")
    return out


def _touch(
    job_id: str,
    progress: str,
    progress_pct: Optional[float] = None,
) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job and job.status == "running":
            job.progress = progress
            if progress_pct is not None:
                # Non tornare indietro: evita salti se Sisal cambia fase (catalogo → dettaglio).
                job.progress_pct = max(float(job.progress_pct), float(progress_pct))
            job.updated_at = time.time()


def _pct_from_sisal(done: int, total: int, label: str) -> float:
    """Mappa done/total Sisal su percentuale UI.

    Con catalogo base (niente dettaglio eventi) il download è ~tutto il lavoro:
    usa 4–96%. Con fase dettaglio, catalogo 4–72% e dettaglio 72–96%.
    """
    label_l = (label or "").lower()
    frac = 0.0 if total <= 0 else min(1.0, max(0.0, done / float(total)))
    if "calcolo" in label_l:
        return 96.0 + frac * 3.0
    if "dettaglio" in label_l:
        return 72.0 + frac * 24.0
    # Catalogo pesato per eventi (done/total = eventi scaricati / eventi totali).
    return 4.0 + frac * 92.0


def _run_job(job_id: str, user_id: str, bonus: BonusForm) -> None:
    try:
        _touch(job_id, "Consulto le Quote", 2.0)

        def on_progress(done: int, total: int, label: str) -> None:
            pct = _pct_from_sisal(done, total, label)
            label_l = (label or "").lower()
            if "calcolo" in label_l:
                display = label or "calcolo puntate…"
            else:
                display = "Consulto le Quote"
            _touch(job_id, display, pct)

        results, notes, is_stub = run_bonus_calculation(
            bonus,
            force_refresh=True,
            progress_callback=on_progress,
        )
        _touch(job_id, "preparo risultati…", 98.0)
        response = CalculationResponse(
            credits_left=credits.get_credits(user_id),
            fetched_at=utc_now(),
            mode="esteso",
            stub=is_stub,
            results=results,
            notes=notes,
        )
        with _LOCK:
            job = _JOBS.get(job_id)
            if job:
                job.status = "done"
                job.progress = "completato"
                job.progress_pct = 100.0
                job.updated_at = time.time()
                job.response = response
    except Exception as exc:  # noqa: BLE001
        credits.refund_credit(user_id, 1)
        with _LOCK:
            job = _JOBS.get(job_id)
            if job:
                job.status = "error"
                job.progress = "errore"
                job.updated_at = time.time()
                job.error = str(exc)


def _prune_locked(max_age_sec: float = 3600.0) -> None:
    now = time.time()
    dead = [jid for jid, j in _JOBS.items() if now - j.created_at > max_age_sec]
    for jid in dead:
        _JOBS.pop(jid, None)
