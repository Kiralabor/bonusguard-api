"""Motore Sisal estratto da quote_desktop.py (senza UI Tkinter).

Usato solo dal backend BonusGuard. Non esporre questo modulo all'app Flutter.
"""

import csv
import math
import os
import re
import threading
import time as time_module
import traceback
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time
from queue import Queue
from typing import Callable, Dict, List, Optional, Sequence, Tuple


from curl_cffi import requests


@dataclass
class MarketOpportunity:
    data: str
    ora: str
    partita: str
    mercato: str
    esiti: List[str]
    quote: List[float]
    url: str = ""
    pal: int = 0
    avv: int = 0


@dataclass(frozen=True)
class EventRef:
    """Riferimento partita per arricchire i mercati via eventDetail."""
    data: str
    ora: str
    partita: str
    url: str
    pal: int
    avv: int


DEFAULT_SISAL_SITE_URL = "https://www.sisal.it/scommesse-matchpoint"
SISAL_EVENT_URL_PREFIX = "https://www.sisal.it/scommesse-matchpoint/evento"
SISAL_API_PREMATCH_BASE = (
    "https://betting.sisal.it/api/lettura-palinsesto-sport/palinsesto/prematch"
)
SISAL_CALCIO_DISCIPLINA = "1"
# Limite richieste parallele verso Sisal (uso personale).
SISAL_API_WORKERS = 8
NETWORK_MODE_NORMAL = "Normale (consigliato)"
NETWORK_MODE_FAST = "Veloce (più carico)"
NETWORK_MODE_SLOW = "Lenta (meno carico)"
NETWORK_MODE_WORKERS = {
    NETWORK_MODE_SLOW: 4,
    NETWORK_MODE_NORMAL: 8,
    NETWORK_MODE_FAST: 16,
}
NETWORK_MODE_CHOICES = (NETWORK_MODE_SLOW, NETWORK_MODE_NORMAL, NETWORK_MODE_FAST)
# Puntata minima accettabile per esito (Sisal tipicamente ≥ 0,50 €).
MIN_STAKE = 0.50
# Il budget può usare solo multipli di 0,05 € (niente 0,01 / 0,03 / 0,07…).
BUDGET_INPUT_STEP = 0.05
SISAL_HTTP_RETRIES = 3
SISAL_HTTP_RETRY_BASE_DELAY = 0.7
# Mercati hedgeabili dal catalogo lista.
# 28319 (DC) non va a 3 vie: si combina con il 1X2 in 3 binari (vedi DOPPIA_CHANCE_PAIRS).
EXCLUSIVE_MARKET_CODES = {3, 18, 7989, 28319}
# Mercati estesi solo da v1/eventDetail (niente corner/marcatori).
DETAIL_MARKET_CODES = {14, 127, 19, 23182, 9942, 165, 166}
# Coppie DC+1X2: (esito DC, esito 1X2 complementare) → mercato UI.
DOPPIA_CHANCE_PAIRS = (
    (("1X", "2"), "Doppia chance 1X / 2"),
    (("12", "X"), "Doppia chance 12 / X"),
    (("X2", "1"), "Doppia chance X2 / 1"),
)
CATALOG_MODE_FAST = "Solo prossimi giorni (veloce)"
CATALOG_MODE_FULL = "Tutte le partite (più lento)"
CATALOG_MODE_CHOICES = (CATALOG_MODE_FAST, CATALOG_MODE_FULL)
CALC_MODE_BASE = "base"
CALC_MODE_EXTENDED = "esteso"
CALC_MODE_LABELS = {
    CALC_MODE_BASE: "Calcolo Base",
    CALC_MODE_EXTENDED: "Calcolo Esteso",
}
ProgressCallback = Optional[Callable[[int, int, str], None]]
FORMATTED_EVENT_DT_RE = re.compile(
    r"(?P<data>\d{1,2}/\d{1,2}/\d{4})\s+ore\s+(?P<ora>\d{1,2}[.:]\d{2})",
    re.IGNORECASE,
)
# Etichette mostrate in tabella (le chiavi restano quelle Sisal in memoria).
OUTCOME_DISPLAY = {
    "1": "1",
    "X": "X",
    "2": "2",
    "1X": "1X",
    "12": "12",
    "X2": "X2",
    "GOAL": "Gol",
    "NOGOAL": "No gol",
    "UNDER": "Under",
    "OVER": "Over",
    "PARI": "Pari",
    "DISPARI": "Dispari",
    "SI": "Sì",
    "NO": "No",
}
# Colori leggibili su sfondo chiaro (testo puntate, via Label tk).
OUTCOME_COLORS = {
    "1": "#1565C0",       # blu
    "X": "#EF6C00",       # arancio
    "2": "#7B1FA2",       # viola
    "1X": "#0277BD",      # azzurro
    "12": "#6A1B9A",      # viola scuro
    "X2": "#D84315",      # arancio-rosso
    "GOAL": "#2E7D32",    # verde
    "NOGOAL": "#C62828",  # rosso
    "UNDER": "#00695C",   # teal
    "OVER": "#AD1457",    # magenta
    "PARI": "#1565C0",    # blu
    "DISPARI": "#EF6C00", # arancio
    "SI": "#2E7D32",      # verde
    "NO": "#C62828",      # rosso
}
def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_team_name(text: str) -> str:
    text = normalize_spaces(text)
    return text.strip(" -–—|")


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("vs.", "-").replace("vs", "-")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return normalize_spaces(text)


def outcome_key(label: str) -> str:
    return normalize_spaces(label).upper()


def display_outcome_label(label: str) -> str:
    return OUTCOME_DISPLAY.get(outcome_key(label), label)


def outcome_color(label: str) -> str:
    return OUTCOME_COLORS.get(outcome_key(label), "#0b3d91")


def format_labeled_values(labels: Sequence[str], values: Sequence[float], suffix: str = "") -> str:
    parts = []
    for label, value in zip(labels, values):
        shown = display_outcome_label(label)
        if suffix:
            parts.append(f"{shown}: {value:.2f}{suffix}")
        else:
            parts.append(f"{shown}: {value:.2f}")
    return " | ".join(parts)


def format_stakes_values(labels: Sequence[str], values: Sequence[float]) -> str:
    """Puntate in testo semplice (CSV / fallback)."""
    parts = []
    for label, value in zip(labels, values):
        shown = display_outcome_label(label)
        parts.append(f"{shown} → €{value:.2f}")
    return "   |   ".join(parts)


def parse_event_date(value: str) -> Optional[datetime]:
    text = normalize_spaces(value)
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_event_time(value: str) -> Optional[time]:
    text = normalize_spaces(value).replace(".", ":")
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def parse_event_datetime(row: dict) -> Optional[datetime]:
    """Data+ora cronologiche per ordinare (gg/mm/aaaa + hh:mm)."""
    day = parse_event_date(str(row.get("Data", "")))
    if day is None:
        return None
    hour = parse_event_time(str(row.get("Ora", ""))) or time(0, 0)
    return datetime.combine(day.date(), hour)


def slugify_sisal(text: str) -> str:
    """Allinea lo slug usato dalle route Matchpoint (evento/:sport/:competition/:regulator)."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def build_sisal_event_url(sport_name: str, competition_name: str, event_name: str) -> str:
    sport = slugify_sisal(sport_name) or "calcio"
    competition = slugify_sisal(competition_name)
    event = slugify_sisal(event_name)
    if not competition or not event:
        return ""
    return f"{SISAL_EVENT_URL_PREFIX}/{sport}/{competition}/{event}"


def deduplicate_opportunities(items: List[MarketOpportunity]) -> List[MarketOpportunity]:
    seen = set()
    unique: List[MarketOpportunity] = []
    for item in items:
        key = (
            normalize_spaces(item.data).lower(),
            item.ora,
            normalize_spaces(item.partita).lower(),
            normalize_spaces(item.mercato).lower(),
            tuple(round(q, 3) for q in item.quote),
            tuple(item.esiti),
        )
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _sisal_quota_to_decimal(raw_quota) -> Optional[float]:
    try:
        value = float(raw_quota) / 100.0
    except (TypeError, ValueError):
        return None
    if 1.0 < value <= 100.0:
        return round(value, 2)
    return None


def _parse_sisal_event_datetime(raw_event: dict) -> Tuple[str, str]:
    formatted = normalize_spaces(str(raw_event.get("formattedDataAvvenimento") or ""))
    match = FORMATTED_EVENT_DT_RE.search(formatted)
    if match:
        return match.group("data"), match.group("ora").replace(".", ":")

    iso = str(raw_event.get("data") or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", iso)
    if m:
        year, month, day, hour, minute = m.groups()
        return f"{day}/{month}/{year}", f"{int(hour)}:{minute}"
    return "", ""


def _event_partita_name(raw_event: dict) -> str:
    partita = clean_team_name(str(raw_event.get("descrizione") or ""))
    if partita:
        return partita

    def competitor_name(value) -> str:
        if isinstance(value, dict):
            return clean_team_name(str(value.get("descrizione") or value.get("name") or ""))
        return clean_team_name(str(value or ""))

    home = competitor_name(raw_event.get("firstCompetitor"))
    away = competitor_name(raw_event.get("secondCompetitor"))
    if home and away:
        return f"{home} - {away}"
    return ""


def _market_label(codice_scommessa: int, info: dict) -> str:
    if codice_scommessa == 3:
        return "1X2 esito finale"
    if codice_scommessa == 18:
        return "Gol sì / Gol no"
    if codice_scommessa == 7989:
        soglia = normalize_spaces(str(info.get("soglia") or ""))
        if soglia:
            return f"Under/Over {soglia}"
        return "Under/Over"
    if codice_scommessa == 14:
        return "1X2 1° tempo"
    if codice_scommessa == 127:
        return "1X2 2° tempo"
    if codice_scommessa == 19:
        return "Pari / Dispari"
    if codice_scommessa == 23182:
        return "Gol sì/no 1° tempo"
    if codice_scommessa == 9942:
        soglia = normalize_spaces(str(info.get("soglia") or ""))
        if soglia:
            return f"Under/Over 1T {soglia}"
        return "Under/Over 1T"
    if codice_scommessa == 165:
        return "Segna casa"
    if codice_scommessa == 166:
        return "Segna ospite"
    return normalize_spaces(str(info.get("descrizione") or f"Mercato {codice_scommessa}"))


def _normalize_outcome_label(label: str) -> str:
    label = normalize_spaces(label).upper()
    if label in {"NO GOAL", "NO GOL"}:
        return "NOGOAL"
    if label == "GOL":
        return "GOAL"
    if label in {"SÌ", "SI"}:
        return "SI"
    return label


def _extract_outcome_odds(info: dict) -> Dict[str, float]:
    odds_by_label: Dict[str, float] = {}
    for esito in info.get("esitoList") or []:
        label = _normalize_outcome_label(str(esito.get("descrizione") or ""))
        odd = _sisal_quota_to_decimal(esito.get("quota"))
        if not label or odd is None:
            continue
        odds_by_label[label] = odd
    return odds_by_label


def _parse_doppia_chance_pairs(
    dc_odds: Dict[str, float],
    mx_odds: Dict[str, float],
) -> List[Tuple[str, List[str], List[float]]]:
    """Hedge binari DC+1X2: 1X vs 2, 12 vs X, X2 vs 1 (stesso payload, zero API extra)."""
    if set(dc_odds) != {"1X", "12", "X2"}:
        return []
    if set(mx_odds) != {"1", "X", "2"}:
        return []

    markets: List[Tuple[str, List[str], List[float]]] = []
    for (left, right), mercato in DOPPIA_CHANCE_PAIRS:
        # Lato DC (1X/12/X2) + lato complementare dal 1X2 (2/X/1).
        left_odd = dc_odds.get(left)
        right_odd = mx_odds.get(right)
        if left_odd is None or right_odd is None:
            continue
        markets.append((mercato, [left, right], [left_odd, right_odd]))
    return markets


def _parse_exclusive_markets(info: dict) -> List[Tuple[str, List[str], List[float]]]:
    """Restituisce 0+ mercati hedgeabili da una infoAggiuntiva Sisal (non DC grezza)."""
    try:
        codice = int(info.get("codiceScommessa"))
    except (TypeError, ValueError):
        return []
    # La DC grezza a 3 vie non è hedgeabile: si espande a parte con il 1X2.
    if codice == 28319 or codice not in EXCLUSIVE_MARKET_CODES:
        return []

    odds_by_label = _extract_outcome_odds(info)
    labels = list(odds_by_label.keys())
    odds = list(odds_by_label.values())

    if len(labels) < 2 or len(labels) != len(odds):
        return []
    if len(set(labels)) != len(labels):
        return []

    # Controlli minimi di forma per i mercati noti.
    if codice == 3 and set(labels) != {"1", "X", "2"}:
        return []
    if codice == 18 and set(labels) != {"GOAL", "NOGOAL"}:
        return []
    if codice == 7989 and set(labels) != {"UNDER", "OVER"}:
        return []

    return [(_market_label(codice, info), labels, odds)]


def _parse_detail_markets(info: dict) -> List[Tuple[str, List[str], List[float]]]:
    """Mercati estesi da eventDetail (esiti mutuamente esclusivi)."""
    try:
        codice = int(info.get("codiceScommessa"))
    except (TypeError, ValueError):
        return []
    if codice not in DETAIL_MARKET_CODES:
        return []

    odds_by_label = _extract_outcome_odds(info)
    labels = list(odds_by_label.keys())
    odds = list(odds_by_label.values())
    if len(labels) < 2 or len(labels) != len(odds):
        return []
    if len(set(labels)) != len(labels):
        return []

    if codice in {14, 127} and set(labels) != {"1", "X", "2"}:
        return []
    if codice == 19 and set(labels) != {"PARI", "DISPARI"}:
        return []
    if codice == 23182 and set(labels) != {"GOAL", "NOGOAL"}:
        return []
    if codice == 9942 and set(labels) != {"UNDER", "OVER"}:
        return []
    if codice in {165, 166} and set(labels) != {"SI", "NO"}:
        return []

    return [(_market_label(codice, info), labels, odds)]


def _opportunities_from_scheda_payload(
    payload: dict,
    catalog_meta: Optional[Dict[str, dict]] = None,
) -> Tuple[List[MarketOpportunity], List[EventRef]]:
    info_map = payload.get("infoAggiuntivaMap") or {}
    opportunities: List[MarketOpportunity] = []
    event_refs: List[EventRef] = []
    seen_events = set()
    disc_map = (catalog_meta or {}).get("disciplinaMap") or {}
    manif_map = (catalog_meta or {}).get("manifestazioneMap") or {}

    for raw_event in payload.get("avvenimentoFeList") or []:
        if int(raw_event.get("codiceDisciplina") or 0) != 1:
            continue
        if raw_event.get("live"):
            continue

        partita = _event_partita_name(raw_event)
        data, ora = _parse_sisal_event_datetime(raw_event)
        if not partita or not ora:
            continue

        pal = raw_event.get("codicePalinsesto")
        avv = raw_event.get("codiceAvvenimento")
        if pal is None or avv is None:
            continue
        try:
            pal_i = int(pal)
            avv_i = int(avv)
        except (TypeError, ValueError):
            continue
        prefix = f"{pal}-{avv}-"

        disc_code = str(raw_event.get("codiceDisciplina") or SISAL_CALCIO_DISCIPLINA)
        man_code = raw_event.get("codiceManifestazione")
        sport_name = str((disc_map.get(disc_code) or {}).get("descrizione") or "Calcio")
        competition_name = ""
        if man_code is not None:
            competition_name = str(
                (manif_map.get(f"{disc_code}-{man_code}") or {}).get("descrizione") or ""
            )
        event_url = build_sisal_event_url(sport_name, competition_name, partita)

        event_type = str(raw_event.get("eventType") or "MATCH").upper()
        event_key = (pal_i, avv_i)
        if event_type == "MATCH" and event_key not in seen_events:
            seen_events.add(event_key)
            event_refs.append(
                EventRef(
                    data=data,
                    ora=ora,
                    partita=partita,
                    url=event_url,
                    pal=pal_i,
                    avv=avv_i,
                )
            )

        mx_odds: Dict[str, float] = {}
        dc_odds: Dict[str, float] = {}

        for key, info in info_map.items():
            if not str(key).startswith(prefix):
                continue
            info = info or {}
            try:
                codice = int(info.get("codiceScommessa"))
            except (TypeError, ValueError):
                codice = None

            if codice == 3:
                mx_odds = _extract_outcome_odds(info)
            elif codice == 28319:
                dc_odds = _extract_outcome_odds(info)

            for mercato, esiti, quote in _parse_exclusive_markets(info):
                opportunities.append(
                    MarketOpportunity(
                        data=data,
                        ora=ora,
                        partita=partita,
                        mercato=mercato,
                        esiti=esiti,
                        quote=quote,
                        url=event_url,
                        pal=pal_i,
                        avv=avv_i,
                    )
                )

        for mercato, esiti, quote in _parse_doppia_chance_pairs(dc_odds, mx_odds):
            opportunities.append(
                MarketOpportunity(
                    data=data,
                    ora=ora,
                    partita=partita,
                    mercato=mercato,
                    esiti=esiti,
                    quote=quote,
                    url=event_url,
                    pal=pal_i,
                    avv=avv_i,
                )
            )
    return opportunities, event_refs


def _sisal_proxy_url() -> str:
    """Proxy di uscita per Sisal (es. IP italiano). Solo traffico Sisal."""
    return (
        os.getenv("SISAL_HTTP_PROXY", "").strip()
        or os.getenv("SISAL_PROXY_URL", "").strip()
    )


def sisal_proxy_configured() -> bool:
    return bool(_sisal_proxy_url())


def _new_sisal_session() -> requests.Session:
    proxy = _sisal_proxy_url()
    if proxy:
        # Preferisci IP IT/residenziale: Render da solo viene spesso bloccato (403).
        return requests.Session(impersonate="chrome", proxy=proxy)
    return requests.Session(impersonate="chrome")


def _build_session_pool(size: int) -> Queue:
    pool: Queue = Queue()
    for _ in range(max(1, size)):
        pool.put(_new_sisal_session())
    return pool


def format_sisal_error(exc: BaseException) -> str:
    """Messaggio chiaro per l'utente quando Sisal non risponde o fallisce."""
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return (
            "Sisal non risponde (timeout). "
            "Riprova tra poco oppure scegli «Lenta (meno carico)»."
        )
    if "403" in lowered or "401" in lowered or "forbidden" in lowered:
        if sisal_proxy_configured():
            return (
                "Sisal ha rifiutato la richiesta anche tramite proxy. "
                "Verifica che il proxy sia italiano e funzionante."
            )
        return (
            "Sisal ha bloccato l'IP del server (serve uscita da IP italiano). "
            "Imposta SISAL_HTTP_PROXY su Render e riprova."
        )
    if "429" in lowered or "too many" in lowered:
        return (
            "Troppe richieste a Sisal. "
            "Attendi un momento e usa la modalità rete «Lenta»."
        )
    if "500" in lowered or "502" in lowered or "503" in lowered or "504" in lowered:
        return "Sisal ha un problema temporaneo sul server. Riprova tra poco."
    if "connection" in lowered or "connect" in lowered or "network" in lowered:
        return "Connessione a Sisal non riuscita. Controlla la rete e riprova."
    if "nessuna opportunità" in lowered or "nessuna competizione" in lowered:
        return text
    return f"Errore durante il download da Sisal: {text}"


def _session_get_json(
    session: requests.Session,
    url: str,
    timeout: float = 60,
    retries: int = SISAL_HTTP_RETRIES,
):
    last_error: Optional[BaseException] = None
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Risposta non valida da Sisal.")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time_module.sleep(SISAL_HTTP_RETRY_BASE_DELAY * attempt)
    assert last_error is not None
    raise last_error


def _merge_scheda_results(
    chunks: Sequence[Tuple[List[MarketOpportunity], List[EventRef]]],
) -> Tuple[List[MarketOpportunity], List[EventRef]]:
    opportunities: List[MarketOpportunity] = []
    event_refs: List[EventRef] = []
    for ops, refs in chunks:
        opportunities.extend(ops)
        event_refs.extend(refs)
    return opportunities, event_refs


def _fetch_scheda_manifestazione(
    session_pool: Queue,
    competition_key: str,
    catalog_meta: Optional[Dict[str, dict]] = None,
) -> Tuple[List[MarketOpportunity], List[EventRef]]:
    session = session_pool.get()
    try:
        url = f"{SISAL_API_PREMATCH_BASE}/schedaManifestazione/0/{competition_key}"
        payload = _session_get_json(session, url)
        return _opportunities_from_scheda_payload(payload, catalog_meta)
    finally:
        session_pool.put(session)


def _fetch_scheda_disciplina(
    session_pool: Queue,
    day_filter: int,
    catalog_meta: Optional[Dict[str, dict]] = None,
) -> Tuple[List[MarketOpportunity], List[EventRef]]:
    session = session_pool.get()
    try:
        url = (
            f"{SISAL_API_PREMATCH_BASE}/schedaDisciplina/"
            f"{day_filter}/{SISAL_CALCIO_DISCIPLINA}"
        )
        payload = _session_get_json(session, url)
        return _opportunities_from_scheda_payload(payload, catalog_meta)
    finally:
        session_pool.put(session)


def _opportunities_from_event_detail_payload(
    payload: dict,
    event: EventRef,
    include_base: bool = False,
    include_extended: bool = True,
) -> List[MarketOpportunity]:
    """Estrae mercati hedgeabili da un payload eventDetail."""
    opportunities: List[MarketOpportunity] = []
    mx_odds: Dict[str, float] = {}
    dc_odds: Dict[str, float] = {}

    for info in (payload.get("infoAggiuntivaMap") or {}).values():
        info = info or {}
        try:
            codice = int(info.get("codiceScommessa"))
        except (TypeError, ValueError):
            codice = None

        if include_base:
            if codice == 3:
                mx_odds = _extract_outcome_odds(info)
            elif codice == 28319:
                dc_odds = _extract_outcome_odds(info)
            for mercato, esiti, quote in _parse_exclusive_markets(info):
                opportunities.append(
                    MarketOpportunity(
                        data=event.data,
                        ora=event.ora,
                        partita=event.partita,
                        mercato=mercato,
                        esiti=esiti,
                        quote=quote,
                        url=event.url,
                        pal=event.pal,
                        avv=event.avv,
                    )
                )

        if include_extended:
            for mercato, esiti, quote in _parse_detail_markets(info):
                opportunities.append(
                    MarketOpportunity(
                        data=event.data,
                        ora=event.ora,
                        partita=event.partita,
                        mercato=mercato,
                        esiti=esiti,
                        quote=quote,
                        url=event.url,
                        pal=event.pal,
                        avv=event.avv,
                    )
                )

    if include_base:
        for mercato, esiti, quote in _parse_doppia_chance_pairs(dc_odds, mx_odds):
            opportunities.append(
                MarketOpportunity(
                    data=event.data,
                    ora=event.ora,
                    partita=event.partita,
                    mercato=mercato,
                    esiti=esiti,
                    quote=quote,
                    url=event.url,
                    pal=event.pal,
                    avv=event.avv,
                )
            )
    return opportunities


def _fetch_event_detail_opportunities(
    session_pool: Queue,
    event: EventRef,
    include_base: bool = False,
    include_extended: bool = True,
) -> List[MarketOpportunity]:
    session = session_pool.get()
    try:
        url = (
            f"{SISAL_API_PREMATCH_BASE}/v1/eventDetail/"
            f"{event.pal}-{event.avv}?metaTplEnabled=true"
        )
        payload = _session_get_json(session, url, timeout=45)
        return _opportunities_from_event_detail_payload(
            payload,
            event,
            include_base=include_base,
            include_extended=include_extended,
        )
    finally:
        session_pool.put(session)


def _dedupe_event_refs(event_refs: Sequence[EventRef]) -> List[EventRef]:
    seen = set()
    unique: List[EventRef] = []
    for ref in event_refs:
        key = (ref.pal, ref.avv)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _enrich_with_event_details(
    session_pool: Queue,
    event_refs: Sequence[EventRef],
    progress_callback: ProgressCallback = None,
    max_workers: int = SISAL_API_WORKERS,
) -> Tuple[List[MarketOpportunity], int]:
    unique_refs = _dedupe_event_refs(event_refs)
    total = len(unique_refs)
    if total == 0:
        return [], 0

    collected: List[MarketOpportunity] = []
    errors = 0
    done = 0
    _report_progress(progress_callback, 0, total, "dettaglio eventi")

    workers = max(1, min(int(max_workers), total))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _fetch_event_detail_opportunities,
                session_pool,
                ref,
                False,
                True,
            ): ref
            for ref in unique_refs
        }
        for future in as_completed(futures):
            done += 1
            try:
                collected.extend(future.result())
                _report_progress(
                    progress_callback, done, total, f"dettaglio evento {done}/{total}"
                )
            except Exception:
                errors += 1
                _report_progress(
                    progress_callback,
                    done,
                    total,
                    f"dettaglio evento {done}/{total} saltato",
                )
    return collected, errors


def _report_progress(callback: ProgressCallback, done: int, total: int, label: str) -> None:
    if callback is None:
        return
    try:
        callback(done, total, label)
    except Exception:
        pass


def _load_alberatura(session: requests.Session) -> dict:
    try:
        session.get(DEFAULT_SISAL_SITE_URL, timeout=20)
    except Exception:
        pass
    return _session_get_json(session, f"{SISAL_API_PREMATCH_BASE}/alberaturaPrematch")


def _catalog_meta_from_alberatura(alberatura: dict) -> Dict[str, dict]:
    return {
        "disciplinaMap": alberatura.get("disciplinaMap") or {},
        "manifestazioneMap": alberatura.get("manifestazioneMap") or {},
    }


def _fetch_events_prossimi_giorni(
    session_pool: Queue,
    alberatura: dict,
    progress_callback: ProgressCallback = None,
    max_workers: int = SISAL_API_WORKERS,
) -> Tuple[List[MarketOpportunity], List[EventRef], int]:
    headers = alberatura.get("headerPalGiornalieriList") or []
    day_filters = []
    for item in headers:
        try:
            day_filter = int(item.get("filter"))
        except (TypeError, ValueError, AttributeError):
            continue
        if day_filter not in day_filters:
            day_filters.append(day_filter)

    if not day_filters:
        day_filters = [1, 4, 3]

    catalog_meta = _catalog_meta_from_alberatura(alberatura)
    total = len(day_filters)
    chunks: List[Tuple[List[MarketOpportunity], List[EventRef]]] = []
    errors = 0
    done = 0
    _report_progress(progress_callback, 0, total, "elenco giornate")

    workers = max(1, min(int(max_workers), total))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _fetch_scheda_disciplina, session_pool, day_filter, catalog_meta
            ): day_filter
            for day_filter in day_filters
        }
        for future in as_completed(futures):
            day_filter = futures[future]
            done += 1
            try:
                chunks.append(future.result())
                _report_progress(progress_callback, done, total, f"giornata {done}")
            except Exception:
                errors += 1
                _report_progress(progress_callback, done, total, f"giornata {done} saltata")
    collected, event_refs = _merge_scheda_results(chunks)
    return collected, event_refs, errors


def _fetch_events_palinsesto_completo(
    session_pool: Queue,
    alberatura: dict,
    progress_callback: ProgressCallback = None,
    max_workers: int = SISAL_API_WORKERS,
) -> Tuple[List[MarketOpportunity], List[EventRef], int]:
    competition_keys = list(
        (alberatura.get("manifestazioneListByDisciplinaTutti") or {}).get(SISAL_CALCIO_DISCIPLINA, []) or []
    )
    event_counts = alberatura.get("eventsNumberByManifestazioneTutti") or {}
    competition_keys = [
        key for key in competition_keys
        if int(event_counts.get(key, 0) or 0) > 0
    ]
    if not competition_keys:
        raise ValueError("Nessuna competizione calcio trovata sulle API Sisal.")

    catalog_meta = _catalog_meta_from_alberatura(alberatura)
    total = len(competition_keys)
    chunks: List[Tuple[List[MarketOpportunity], List[EventRef]]] = []
    errors = 0
    done = 0
    _report_progress(progress_callback, 0, total, "campionati")

    workers = max(1, min(int(max_workers), total))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _fetch_scheda_manifestazione, session_pool, key, catalog_meta
            ): key
            for key in competition_keys
        }
        for future in as_completed(futures):
            key = futures[future]
            done += 1
            try:
                chunks.append(future.result())
                _report_progress(progress_callback, done, total, f"campionato {done}/{total}")
            except Exception:
                errors += 1
                _report_progress(progress_callback, done, total, f"campionato {done}/{total} saltato")
    collected, event_refs = _merge_scheda_results(chunks)
    return collected, event_refs, errors


def fetch_sisal_calcio_opportunities(
    catalog_mode: str = CATALOG_MODE_FAST,
    progress_callback: ProgressCallback = None,
    include_extended: bool = False,
    max_workers: int = SISAL_API_WORKERS,
) -> Tuple[List[MarketOpportunity], int]:
    """Scarica opportunità hedgeabili.

    Base: solo catalogo lista (1X2, GG/NG, U/O, DC a 2 vie).
    Esteso: aggiunge mercati da v1/eventDetail per ogni partita.
    Restituisce (opportunità, richieste fallite dopo i retry).
    """
    workers = max(1, int(max_workers))
    bootstrap = _new_sisal_session()
    try:
        alberatura = _load_alberatura(bootstrap)
    except Exception as exc:
        raise RuntimeError(format_sisal_error(exc)) from exc

    if include_extended:
        pool_size = workers if catalog_mode == CATALOG_MODE_FULL else min(workers, 8)
    else:
        pool_size = workers if catalog_mode == CATALOG_MODE_FULL else min(workers, 4)
    session_pool = _build_session_pool(pool_size)
    session_pool.put(bootstrap)

    try:
        if catalog_mode == CATALOG_MODE_FULL:
            collected, event_refs, errors = _fetch_events_palinsesto_completo(
                session_pool, alberatura, progress_callback, max_workers=workers
            )
        else:
            collected, event_refs, errors = _fetch_events_prossimi_giorni(
                session_pool, alberatura, progress_callback, max_workers=workers
            )

        detail_ops: List[MarketOpportunity] = []
        if include_extended:
            detail_ops, detail_errors = _enrich_with_event_details(
                session_pool,
                event_refs,
                progress_callback,
                max_workers=workers,
            )
            errors += detail_errors

        opportunities = deduplicate_opportunities(collected + detail_ops)
        if not opportunities:
            detail = f" ({errors} richieste non leggibili)" if errors else ""
            raise ValueError(
                f"Nessuna opportunità di mercato trovata sulle API Sisal.{detail}"
            )
        return opportunities, errors
    except RuntimeError:
        raise
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(format_sisal_error(exc)) from exc


def ceil_to_int(x: float) -> int:
    return int(math.ceil(x - 1e-12))


def _money(value: float) -> float:
    return round(float(value) + 1e-12, 2)


def _is_multiple_of_step(amount: float, step: float) -> bool:
    """True se l'importo è un multiplo giocabile dello step (confronto in centesimi)."""
    step_cents = int(round(float(step) * 100))
    if step_cents <= 0:
        return False
    amount_cents = int(round(float(amount) * 100))
    return amount_cents % step_cents == 0


def _format_budget_for_step(amount: float, step: float) -> str:
    """Scrive il budget in modo coerente con lo step (con 1 € senza centesimi)."""
    amount = _money(amount)
    step = _money(step)
    if step >= 1.0 - 1e-12:
        return str(int(round(amount)))
    if step >= 0.5 - 1e-12:
        return f"{amount:.2f}"
    return f"{amount:.2f}"


def _budget_step_rule_text(step: float) -> str:
    step = _money(step)
    if step >= 1.0 - 1e-12:
        return "solo euro interi, senza centesimi (10, 11, 12…)"
    if step >= 0.5 - 1e-12:
        return "solo multipli di 0,50 € (10,00 / 10,50 / 11,00…)"
    if step >= 0.1 - 1e-12:
        return "solo multipli di 0,10 € (10,00 / 10,10 / 10,20…; niente 0,05)"
    return "solo multipli di 0,05 € (niente 0,01 / 0,03 / 0,07…)"


def optimize_stakes(
    budget: float,
    odds: Sequence[float],
    step: float = 0.05,
    min_stake: float = MIN_STAKE,
) -> dict:
    """Distribuisce il budget su N esiti esclusivi massimizzando il ritorno minimo.

    Tutte le puntate sono multipli dello step (giocabili su Sisal).
    Se il budget non è multiplo dello step, si usa solo la parte arrotondabile
    per difetto (es. 10,03 € con step 0,05 → si giocano 10,00 €).
    """
    odds_list = [float(q) for q in odds]
    budget = _money(budget)
    step = _money(step)
    min_stake = _money(min_stake) if min_stake > 0 else 0.0

    if len(odds_list) < 2:
        raise ValueError("Servono almeno due quote.")
    if any(q <= 1.0 for q in odds_list):
        raise ValueError("Tutte le quote devono essere maggiori di 1.")
    if step <= 0:
        raise ValueError("L'arrotondamento deve essere maggiore di zero.")

    # Budget giocabile: solo multipli interi dello step (mai sopra il budget).
    total_units = int(math.floor(budget / step + 1e-12))
    usable_budget = _money(total_units * step)
    if total_units <= 0 or usable_budget <= 0:
        raise ValueError("Budget troppo basso per l'arrotondamento scelto.")

    n = len(odds_list)
    # Il minimo per esito deve essere a sua volta un multiplo dello step.
    min_units = ceil_to_int(min_stake / step) if min_stake > 0 else 0
    playable_min = _money(min_units * step)
    if min_units > 0 and n * min_units > total_units:
        raise ValueError(
            f"Budget insufficiente: servono almeno {n * playable_min:.2f} € "
            f"({n} esiti × minimo giocabile {playable_min:.2f} €)."
        )

    low = 0.0
    high = max(odds_list) * usable_budget
    best_units = [min_units] * n

    for _ in range(70):
        target_return = (low + high) / 2
        required_units = [
            max(min_units, ceil_to_int((target_return / q) / step))
            for q in odds_list
        ]
        if sum(required_units) <= total_units:
            low = target_return
            best_units = required_units
        else:
            high = target_return

    units = best_units[:]
    leftover = total_units - sum(units)
    while leftover > 0:
        current_returns = [units[i] * step * odds_list[i] for i in range(n)]
        lowest_index = min(range(n), key=lambda i: current_returns[i])
        units[lowest_index] += 1
        leftover -= 1

    stakes = [_money(u * step) for u in units]
    if any(not _is_multiple_of_step(s, step) for s in stakes):
        raise ValueError("Puntata non allineata all'arrotondamento (non giocabile).")
    if any(s < playable_min - 1e-9 for s in stakes) or any(s <= 0 for s in stakes):
        raise ValueError(
            f"Puntata sotto il minimo accettabile ({playable_min:.2f} €) o pari a zero."
        )
    spent = _money(sum(stakes))
    if abs(spent - usable_budget) > 1e-9:
        raise ValueError("Le puntate non sommano al budget giocabile.")

    returns = [_money(stakes[i] * odds_list[i]) for i in range(n)]
    min_return = min(returns)
    loss = _money(spent - min_return)
    loss_pct = round((loss / spent) * 100, 4) if spent else 0.0

    return {
        "puntate": stakes,
        "ritorno_minimo": min_return,
        "perdita": loss,
        "perdita_%": loss_pct,
        "budget_usato": spent,
    }


def opportunity_meets_minimum_odd(item: MarketOpportunity, minimum_odd: Optional[float]) -> bool:
    if minimum_odd is None:
        return True
    return min(item.quote) >= minimum_odd


def row_meets_minimum_odd(row: dict, minimum_odd: Optional[float]) -> bool:
    if minimum_odd is None:
        return True
    quotes = row.get("_quote") or []
    if not quotes:
        return True
    return min(float(q) for q in quotes) >= minimum_odd


def rank_opportunities(
    opportunities: List[MarketOpportunity],
    budget: float,
    step: float,
    top_n: int,
    min_stake: float = MIN_STAKE,
) -> Tuple[List[dict], int]:
    rows = []
    skipped_min_stake = 0
    for item in opportunities:
        try:
            calc = optimize_stakes(budget, item.quote, step, min_stake=min_stake)
        except ValueError:
            skipped_min_stake += 1
            continue
        rows.append(
            {
                "Data": item.data,
                "Ora": item.ora,
                "Partita": item.partita,
                "Tipo scommessa": item.mercato,
                "Quote Sisal": format_labeled_values(item.esiti, item.quote),
                "Quanto puntare": format_stakes_values(item.esiti, calc["puntate"]),
                "Rientro minimo": calc["ritorno_minimo"],
                "Perdita €": calc["perdita"],
                "Perdita %": calc["perdita_%"],
                "_esiti": list(item.esiti),
                "_quote": list(item.quote),
                "_puntate": list(calc["puntate"]),
                "_url": item.url,
                "_pal": int(item.pal or 0),
                "_avv": int(item.avv or 0),
            }
        )

    # Obiettivo principale: minor perdita percentuale.
    rows.sort(key=lambda r: (r["Perdita %"], -r["Rientro minimo"], r["Perdita €"]))
    return rows[:top_n], skipped_min_stake


