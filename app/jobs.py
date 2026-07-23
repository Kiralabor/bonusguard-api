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
    }
    if job.error:
        out["error"] = job.error
    if job.response is not None:
        out["result"] = job.response.model_dump(mode="json")
    return out


def _touch(job_id: str, progress: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job and job.status == "running":
            job.progress = progress
            job.updated_at = time.time()


def _run_job(job_id: str, user_id: str, bonus: BonusForm) -> None:
    try:
        _touch(job_id, "download quote Sisal…")
        results, notes, is_stub = run_bonus_calculation(bonus, force_refresh=True)
        _touch(job_id, "calcolo puntate…")
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
