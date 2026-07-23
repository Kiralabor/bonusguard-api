"""Rate limit semplice in-memory (best-effort su singolo worker)."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict

from fastapi import HTTPException, Request

_LOCK = Lock()
_HITS: Dict[str, Deque[float]] = defaultdict(deque)


def check_rate_limit(
    key: str,
    *,
    max_hits: int,
    window_sec: float,
) -> None:
    now = time.time()
    with _LOCK:
        q = _HITS[key]
        while q and now - q[0] > window_sec:
            q.popleft()
        if len(q) >= max_hits:
            raise HTTPException(
                status_code=429,
                detail="Troppe richieste. Riprova tra poco.",
            )
        q.append(now)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"
