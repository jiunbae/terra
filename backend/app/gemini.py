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

_ROTATE_STATUS = {429, 500, 503}


class GeminiError(Exception):
    pass


def _load_keys() -> list[str]:
    raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        msg = ("Gemini API 키가 비어 있습니다. `.env`에 GEMINI_API_KEYS를 설정하거나 "
               "키 저장소가 연결된 환경에서 start.sh로 실행하세요.")
        raise GeminiError(msg)
    return keys


_key_cycle: itertools.cycle[str] | None = None


def _next_key() -> str:
    global _key_cycle
    if _key_cycle is None:
        _key_cycle = itertools.cycle(_load_keys())
    return next(_key_cycle)


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
    async with httpx.AsyncClient(timeout=120) as client:
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
                continue

            if resp.status_code in _ROTATE_STATUS:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                log.warning("키 #%d 응답 %d — 로테이션", attempt % n_keys + 1, resp.status_code)
                if attempt >= n_keys - 1:
                    await asyncio.sleep(2)
                continue

            if resp.status_code != 200:
                raise GeminiError(f"Gemini API 오류 HTTP {resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            try:
                candidate = data["candidates"][0]
                text = "".join(
                    p.get("text", "") for p in candidate["content"]["parts"]
                )
                return json.loads(text)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                finish = data.get("candidates", [{}])[0].get("finishReason", "?")
                raise GeminiError(
                    f"Gemini 응답 파싱 실패 (finishReason={finish}): {e}"
                ) from e

    raise GeminiError(f"모든 API 키 쿼터/오류로 실패했습니다. 마지막 오류: {last_err}")
