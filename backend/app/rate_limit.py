"""임시 공개 데모를 위한 가벼운 메모리 기반 요청 제한."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        max_buckets: int = 8192,
        trusted_proxy_ips: set[str] | None = None,
    ) -> None:
        if limit < 1 or window_seconds < 1 or max_buckets < 1:
            raise ValueError("rate limiter settings must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        configured_proxies = os.environ.get("TERRA_TRUSTED_PROXY_IPS") or os.environ.get(
            "TERRA_FORWARDED_ALLOW_IPS", "127.0.0.1,::1"
        )
        self._trusted_proxy_ips = (
            trusted_proxy_ips
            if trusted_proxy_ips is not None
            else {value.strip() for value in configured_proxies.split(",") if value.strip()}
        )
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._checks = 0
        self._lock = asyncio.Lock()

    def _request_key(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("cf-connecting-ip")
        # CF-Connecting-IP is caller-controlled unless the direct peer is our
        # configured tunnel/reverse proxy. Accept only one syntactically valid IP.
        if forwarded and peer in self._trusted_proxy_ips:
            try:
                return str(ipaddress.ip_address(forwarded.strip()))
            except ValueError:
                pass
        return peer

    async def check(self, request: Request, *, key_override: str | None = None) -> None:
        # 신뢰한 Cloudflare Tunnel만 전달 IP를 쓴다. 직접 접근은 socket IP로 제한한다.
        key = key_override or self._request_key(request)
        now = time.monotonic()
        async with self._lock:
            self._checks += 1
            cutoff = now - self.window_seconds
            # 다시 찾아오지 않는 IP의 만료된 버킷이 무한히 쌓이지 않도록 주기적으로 정리한다.
            if len(self._hits) > 1024 and self._checks % 256 == 0:
                stale = [k for k, d in self._hits.items() if not d or d[-1] < cutoff]
                for k in stale:
                    del self._hits[k]
            if key not in self._hits and len(self._hits) >= self.max_buckets:
                stale = [k for k, d in self._hits.items() if not d or d[-1] < cutoff]
                for stale_key in stale:
                    del self._hits[stale_key]
            if key not in self._hits and len(self._hits) >= self.max_buckets:
                # 상한 이후의 새 클라이언트는 제한을 우회하도록 기존 버킷을
                # 계속 축출하지 않고 하나의 보수적인 overflow 버킷을 공유한다.
                key = "__overflow__"
                if key not in self._hits:
                    oldest = next(iter(self._hits))
                    del self._hits[oldest]
            hits = self._hits[key]
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
