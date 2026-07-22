"""Motore calcolo — STUB.

TODO: collegare la logica di quote_desktop.py (fetch Sisal esteso + optimize_stakes)
restando SOLO su questo server. L'app Flutter non deve mai vedere gli URL Sisal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from .models import BonusForm, StakeProposal


def run_extended_calculation(bonus: BonusForm) -> tuple[List[StakeProposal], List[str]]:
    """Restituisce proposte fittizie per sviluppare UI e flussi crediti."""
    budget = round(float(bonus.playable_budget), 2)
    step = float(bonus.step)
    notes = [
        "Risultato STUB: nessuna chiamata a Sisal.",
        f"Budget usato nel mock: {budget:.2f} € · step {step:.2f} €.",
        f"Bonus dichiarato: {bonus.bonus_amount:.2f} € · rollover x{bonus.rollover_multiplier:g}.",
        "In produzione qui girerà la scansione estesa + optimize_stakes.",
    ]

    # Puntate allineate allo step (come regola desktop).
    def align(parts: List[float]) -> List[float]:
        units = [max(1, int(round(p / step))) for p in parts]
        # Ribilancia alla somma budget in unità
        total_units = int(round(budget / step))
        while sum(units) > total_units and max(units) > 1:
            i = units.index(max(units))
            units[i] -= 1
        while sum(units) < total_units:
            i = units.index(min(units))
            units[i] += 1
        return [round(u * step, 2) for u in units]

    samples = [
        {
            "data": "22/07/2026",
            "ora": "20:45",
            "partita": "Esempio Casa - Esempio Ospite",
            "mercato": "1X2 esito finale",
            "esiti": ["1", "X", "2"],
            "quote": [1.70, 4.25, 4.25],
            "weights": [0.55, 0.225, 0.225],
            "open_url": "https://www.sisal.it/scommesse-matchpoint",
        },
        {
            "data": "22/07/2026",
            "ora": "18:30",
            "partita": "Demo United - Sample City",
            "mercato": "Gol sì / Gol no",
            "esiti": ["GOAL", "NOGOAL"],
            "quote": [1.85, 1.95],
            "weights": [0.51, 0.49],
            "open_url": "https://www.sisal.it/scommesse-matchpoint",
        },
        {
            "data": "23/07/2026",
            "ora": "21:00",
            "partita": "Alpha FC - Beta FC",
            "mercato": "Under/Over 2.5",
            "esiti": ["UNDER", "OVER"],
            "quote": [1.90, 1.90],
            "weights": [0.50, 0.50],
            "open_url": "https://www.sisal.it/scommesse-matchpoint",
        },
    ]

    results: List[StakeProposal] = []
    for item in samples[: bonus.top_n]:
        raw = [budget * w for w in item["weights"]]
        puntate = align(raw)
        returns = [round(puntate[i] * item["quote"][i], 2) for i in range(len(puntate))]
        min_ret = min(returns) if returns else 0.0
        loss = round(sum(puntate) - min_ret, 2)
        loss_pct = round((loss / sum(puntate)) * 100, 2) if sum(puntate) else 0.0
        results.append(
            StakeProposal(
                data=item["data"],
                ora=item["ora"],
                partita=item["partita"],
                mercato=item["mercato"],
                esiti=item["esiti"],
                quote=item["quote"],
                puntate=puntate,
                rientro_minimo=min_ret,
                perdita_euro=loss,
                perdita_pct=loss_pct,
                open_url=item["open_url"],
            )
        )

    results.sort(key=lambda r: (r.perdita_pct, r.perdita_euro))
    return results, notes


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
