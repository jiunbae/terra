"""임시 공개 데모를 위한 가벼운 메모리 기반 요청 제한."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, request: Request, *, key_override: str | None = None) -> None:
        # Cloudflare Tunnel이 붙이는 원본 IP. 로컬 개발 시에는 socket IP를 쓴다.
        key = key_override or request.headers.get("cf-connecting-ip") or (
            request.client.host if request.client else "unknown"
        )
        now = time.monotonic()
        async with self._lock:
            hits = self._hits[key]
            cutoff = now - self.window_seconds
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])))
                raise HTTPException(
                    status_code=429,
                    detail=f"요청 한도를 초과했습니다. {retry_after}초 후 다시 시도해 주세요.",
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)
