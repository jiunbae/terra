"""Small ASGI middlewares for public-demo HTTP hardening."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ContentLengthLimitMiddleware:
    """Reject oversized state-changing requests before JSON parsing.

    Terra has no upload endpoint and its largest accepted source text is 100k
    characters. A one-megabyte default leaves generous JSON overhead while
    preventing an anonymous client from making Pydantic buffer huge bodies.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes < 1024:
            raise ValueError("max_bytes must be at least 1024")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}:
            raw_length = dict(scope.get("headers", [])).get(b"content-length")
            if raw_length is not None:
                try:
                    length = int(raw_length)
                except ValueError:
                    await JSONResponse(
                        {"detail": "올바르지 않은 Content-Length 헤더입니다."},
                        status_code=400,
                    )(scope, receive, send)
                    return
                if length > self.max_bytes:
                    await JSONResponse(
                        {"detail": f"요청 본문은 {self.max_bytes}바이트를 초과할 수 없습니다."},
                        status_code=413,
                    )(scope, receive, send)
                    return

            # Content-Length is optional (for example with chunked transfer), so
            # enforce the limit against the actual ASGI body before handing it to
            # FastAPI/Pydantic. At most max_bytes are retained in memory.
            messages: list[Message] = []
            total = 0
            more_body = True
            while more_body:
                message = await receive()
                messages.append(message)
                if message["type"] == "http.disconnect":
                    break
                if message["type"] != "http.request":
                    continue
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await JSONResponse(
                        {"detail": f"요청 본문은 {self.max_bytes}바이트를 초과할 수 없습니다."},
                        status_code=413,
                    )(scope, receive, send)
                    return
                more_body = bool(message.get("more_body", False))

            index = 0

            async def replay_receive() -> Message:
                nonlocal index
                if index < len(messages):
                    message = messages[index]
                    index += 1
                    return message
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_receive, send)
            return
        await self.app(scope, receive, send)


class ProductionHeadersMiddleware:
    """Apply browser hardening and deterministic cache policy to responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        async def send_with_headers(message: MutableMapping[str, Any] | Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("x-frame-options", "DENY")
                headers.setdefault("referrer-policy", "strict-origin-when-cross-origin")
                headers.setdefault(
                    "permissions-policy",
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
                )
                headers.setdefault("cross-origin-opener-policy", "same-origin")
                headers.setdefault(
                    "strict-transport-security", "max-age=31536000; includeSubDomains"
                )
                # Swagger/ReDoc(개발 전용 — 프로덕션에서는 docs가 비활성)는 CDN 스크립트와
                # 인라인 부트스트랩을 쓰므로 해당 경로에서만 CSP를 완화한다. 그 외 모든
                # 응답은 엄격한 self 기반 정책을 유지한다.
                if path in {"/docs", "/redoc"}:
                    headers.setdefault(
                        "content-security-policy",
                        "default-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                        "img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https:; "
                        "script-src 'self' 'unsafe-inline' https:; "
                        "connect-src 'self' https:; worker-src 'self' blob:",
                    )
                else:
                    headers.setdefault(
                        "content-security-policy",
                        "default-src 'self'; base-uri 'self'; object-src 'none'; "
                        "frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
                        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                        "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:",
                    )

                if path.startswith(("/assets/", "/generated/")):
                    headers["cache-control"] = "public, max-age=31536000, immutable"
                elif path == "/favicon.svg":
                    headers["cache-control"] = "public, max-age=86400"
                elif path.startswith("/api/"):
                    headers["cache-control"] = "no-store"
                else:
                    headers["cache-control"] = "no-cache"
            await send(message)  # type: ignore[arg-type]

        await self.app(scope, receive, send_with_headers)
