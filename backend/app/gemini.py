"""Gemini API 클라이언트 — 무료 키 N개 라운드로빈 로테이션.

키는 환경변수 GEMINI_API_KEYS(콤마 구분)로 주입된다. 코드/저장소에 키를 두지 않는다.
쿼터 초과(429)나 일시 오류(5xx) 시 다음 키로 넘어가며, 전 키 소진 시 짧은 백오프 후 한 바퀴 더 돈다.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("terra.gemini")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")
BASE = "https://generativelanguage.googleapis.com/v1beta"

# 429(쿼터)·5xx(일시 오류)뿐 아니라 401/403(키 만료·권한 없음)도 다음 키로 넘긴다.
# 하나의 죽은 키가 전체 요청을 중단시키지 않도록 한다. 400은 요청 자체 오류이므로 로테이션하지 않는다.
_ROTATE_STATUS = {401, 403, 408, 429, 500, 502, 503, 504}


class GeminiError(Exception):
    pass


def _load_keys() -> list[str]:
    raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY", "")
    # 중복 키는 실제 장애 시 같은 요청을 반복할 뿐이므로 순서를 보존해 제거한다.
    keys = list(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))
    if not keys:
        msg = ("Gemini API 키가 비어 있습니다. `.env`에 GEMINI_API_KEYS를 설정하거나 "
               "키 저장소가 연결된 환경에서 start.sh로 실행하세요.")
        raise GeminiError(msg)
    return keys


_key_cycle: itertools.cycle[str] | None = None
_key_snapshot: tuple[str, ...] = ()


def _next_key() -> str:
    global _key_cycle, _key_snapshot
    keys = tuple(_load_keys())
    # 테스트/운영 중 키 저장소가 갱신되면 프로세스 재시작 없이 새 목록을 쓴다.
    if _key_cycle is None or keys != _key_snapshot:
        _key_snapshot = keys
        _key_cycle = itertools.cycle(keys)
    return next(_key_cycle)


def _retry_delay() -> float:
    try:
        value = float(os.environ.get("TERRA_GEMINI_RETRY_DELAY", "2"))
    except ValueError:
        return 2.0
    return max(0.0, min(10.0, value))


async def generate_json(
    system: str,
    user_text: str,
    response_schema: dict[str, Any],
    temperature: float = 0.3,
    max_output_tokens: int = 65536,
) -> dict[str, Any]:
    """구조화 출력(generateContent + responseSchema)으로 JSON을 생성한다."""
    n_keys = len(_load_keys())
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }

    last_err: str = ""
    timeout = httpx.Timeout(120, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 키당 최대 2바퀴 시도
        for attempt in range(n_keys * 2):
            key = _next_key()
            try:
                resp = await client.post(
                    f"{BASE}/models/{MODEL}:generateContent",
                    headers={"x-goog-api-key": key},
                    json=body,
                )
            except httpx.HTTPError as e:
                last_err = f"network: {e}"
                log.warning("Gemini 네트워크 오류, 다음 키로 로테이션: %s", e)
                if (attempt + 1) % n_keys == 0 and attempt + 1 < n_keys * 2:
                    await asyncio.sleep(_retry_delay())
                continue

            if resp.status_code in _ROTATE_STATUS:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                log.warning("키 #%d 응답 %d — 로테이션", attempt % n_keys + 1, resp.status_code)
                # 모든 키를 한 번 사용한 뒤에만 백오프한다. 기존 구현처럼 두 번째
                # 라운드의 매 요청마다 sleep하지 않아 장애 복구 지연이 누적되지 않는다.
                if (attempt + 1) % n_keys == 0 and attempt + 1 < n_keys * 2:
                    await asyncio.sleep(_retry_delay())
                continue

            if resp.status_code != 200:
                raise GeminiError(f"Gemini API 오류 HTTP {resp.status_code}: {resp.text[:500]}")

            data: dict[str, Any] = {}
            try:
                parsed = resp.json()
                if not isinstance(parsed, dict):
                    raise ValueError("response JSON is not an object")
                data = parsed
                candidate = data["candidates"][0]
                text = "".join(
                    p.get("text", "") for p in candidate["content"]["parts"]
                )
                result = json.loads(text)
                if not isinstance(result, dict):
                    raise ValueError("generated JSON is not an object")
                return result
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as e:
                candidates = data.get("candidates")
                finish = (
                    candidates[0].get("finishReason", "?")
                    if isinstance(candidates, list)
                    and candidates
                    and isinstance(candidates[0], dict)
                    else "?"
                )
                raise GeminiError(
                    f"Gemini 응답 파싱 실패 (finishReason={finish}): {e}"
                ) from e

    raise GeminiError(f"모든 API 키 쿼터/오류로 실패했습니다. 마지막 오류: {last_err}")
