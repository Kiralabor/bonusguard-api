from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class MeResponse(BaseModel):
    user_id: str
    email: Optional[str] = None
    credits: int
    phone_verified: bool = False


class BonusForm(BaseModel):
    """Dati bonus inseriti dall'utente (v1)."""

    bonus_amount: float = Field(..., gt=0, description="Importo bonus erogato (€)")
    playable_budget: float = Field(
        ...,
        gt=0,
        description="Soldi da usare nel calcolo / stake totale giocabile (€)",
    )
    rollover_multiplier: float = Field(
        5.0,
        ge=1.0,
        description="Moltiplicatore requisito di giocata (es. 5 = x5)",
    )
    min_odd: Optional[float] = Field(
        1.50,
        description="Ignora quote sotto questa soglia (None = nessun filtro)",
    )
    step: float = Field(
        0.05,
        description="Arrotondamento puntate: 0.05 / 0.10 / 0.50 / 1.00",
    )
    top_n: int = Field(50, ge=1, le=100)


class StakeProposal(BaseModel):
    data: str
    ora: str
    partita: str
    mercato: str
    esiti: List[str]
    quote: List[float]
    puntate: List[float]
    rientro_minimo: float
    perdita_euro: float
    perdita_pct: float
    open_url: str = ""


class CalculationRequest(BaseModel):
    bonus: BonusForm
    # Token Supabase JWT (in produzione obbligatorio).
    access_token: Optional[str] = None


class CalculationResponse(BaseModel):
    credits_left: int
    fetched_at: datetime
    mode: str = "esteso"
    stub: bool = True
    disclaimer: str = (
        "Strumento di assistenza: non garantisce prelievo o guadagno. "
        "Non ufficiale Sisal. Le quote cambiano."
    )
    results: List[StakeProposal]
    notes: List[str] = Field(default_factory=list)


class CalculationJobStart(BaseModel):
    job_id: str
    status: str = "running"
    credits_left: int
    message: str = (
        "Scansione avviata. Attendere: su Render Free può richiedere alcuni minuti."
    )


class CalculationJobStatus(BaseModel):
    job_id: str
    status: str
    error: Optional[str] = None
    result: Optional[CalculationResponse] = None


class CreditPackage(BaseModel):
    id: str
    credits: int
    price_eur: float


class PackagesResponse(BaseModel):
    packages: List[CreditPackage]
