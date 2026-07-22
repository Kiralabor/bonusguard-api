"""Gateway locale IT: esegue solo la scansione Sisal (IP del PC italiano).

Esposto via Cloudflare Tunnel; Render chiama questo servizio.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import sisal_engine as eng

app = FastAPI(title="BonusGuard Sisal Worker IT", version="1.0.0")


class ScanRequest(BaseModel):
    catalog_mode: str = Field(default="Solo prossimi giorni (veloce)")
    include_extended: bool = True
    max_workers: int = 6


def _check_secret(authorization: str | None) -> None:
    expected = os.getenv("SISAL_WORKER_SECRET", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Worker secret non configurato.")
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Non autorizzato.")


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "bonusguard-sisal-worker-it",
        "secret_configured": bool(os.getenv("SISAL_WORKER_SECRET", "").strip()),
    }


@app.post("/scan")
def scan(body: ScanRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_secret(authorization)
    try:
        ops, errors = eng.fetch_sisal_calcio_opportunities(
            catalog_mode=body.catalog_mode,
            include_extended=body.include_extended,
            max_workers=max(1, int(body.max_workers)),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "errors": int(errors),
        "count": len(ops),
        "opportunities": [asdict(item) for item in ops],
    }
